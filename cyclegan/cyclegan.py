import argparse
import os
import numpy as np
import math
import itertools
import datetime
import time

import torchvision.transforms as transforms
from torchvision.utils import save_image, make_grid

from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable
import sc_model
from models import *
from datasets import *
from utils import *

import torch.nn as nn
import torch.nn.functional as F
import torch
# from torch.utils.tensorboard import SummaryWriter
# writer = SummaryWriter()
# 定义参数
parser = argparse.ArgumentParser()
parser.add_argument("--epoch", type=int, default=0, help="epoch to start training from")
parser.add_argument("--n_epochs", type=int, default=50, help="number of epochs of training")
parser.add_argument("--dataset_name", type=str, default="dataset", help="name of the dataset")
parser.add_argument("--batch_size", type=int, default=1, help="size of the batches")
parser.add_argument("--lr", type=float, default=0.0002, help="adam: learning rate")
parser.add_argument("--b1", type=float, default=0.5, help="adam: decay of first order momentum of gradient")
parser.add_argument("--b2", type=float, default=0.999, help="adam: decay of first order momentum of gradient")
parser.add_argument("--decay_epoch", type=int, default=25, help="epoch from which to start lr decay")
parser.add_argument("--n_cpu", type=int, default=1, help="number of cpu threads to use during batch generation")
parser.add_argument("--img_height", type=int, default=256, help="size of image height")
parser.add_argument("--img_width", type=int, default=256, help="size of image width")
parser.add_argument("--channels", type=int, default=3, help="number of image channels")
parser.add_argument("--sample_interval", type=int, default=2000, help="interval between saving generator outputs")
parser.add_argument("--checkpoint_interval", type=int, default=10, help="interval between saving model checkpoints")
parser.add_argument("--n_residual_blocks", type=int, default=9, help="number of residual blocks in generator")
parser.add_argument("--lambda_adv", type=float, default=3.0, help="adv loss weight")
parser.add_argument("--lambda_cyc", type=float, default=8.0, help="cycle loss weight")
parser.add_argument("--lambda_id", type=float, default=0.0, help="identity loss weight")
parser.add_argument("--lambda_ct", type=float, default=0.0, help="context loss weight")
parser.add_argument("--lambda_sc", type=float, default=10.0, help="Spatial Correlative loss weight")
parser.add_argument('--warmup_epoches',type=int,default=0,help='number of epoches without adv loss')
parser.add_argument('--attn_layers', type=str, default='4, 7, 9', help='compute spatial loss on which layers')
parser.add_argument('--patch_nums', type=float, default=256, help='select how many patches for shape consistency, -1 use all')
parser.add_argument('--patch_size', type=int, default=64, help='patch size to calculate the attention')
parser.add_argument('--loss_mode', type=str, default='cos', help='which loss type is used, cos | l1 | info')
parser.add_argument('--use_norm', action='store_true', help='normalize the feature map for FLSeSim')
parser.add_argument('--learned_attn', action='store_true', help='use the learnable attention map')
parser.add_argument('--T', type=float, default=0.07, help='temperature for similarity')
opt = parser.parse_args()
print(opt)
# 创建采样图像保存路径和模型参数保存路径
os.makedirs("images/%s" % opt.dataset_name, exist_ok=True)
os.makedirs("saved_models/%s" % opt.dataset_name, exist_ok=True)

# 损失函数
criterion_GAN = torch.nn.MSELoss()
criterion_cycle = torch.nn.L1Loss()
criterion_identity = torch.nn.L1Loss()
content_loss = torch.nn.L1Loss()

input_shape = (opt.channels, opt.img_height, opt.img_width)

# 初始化生成器和鉴别器
G_AB = GeneratorResNet(input_shape, opt.n_residual_blocks)
G_BA = GeneratorResNet(input_shape, opt.n_residual_blocks)
D_A = Discriminator(input_shape)
D_B = Discriminator(input_shape)
# 定义特征提取器
encoder = FeatureExtractor()
encoder.eval()

cuda = torch.cuda.is_available()
#cuda
if cuda:
    G_AB = G_AB.cuda()
    G_BA = G_BA.cuda()
    D_A = D_A.cuda()
    D_B = D_B.cuda()
    netPre = sc_model.VGG16().cuda()
    criterion_GAN.cuda()
    criterion_cycle.cuda()
    criterion_identity.cuda()
    content_loss.cuda()
if opt.epoch != 0:
    # 如果epoch不是从0 开始，则 Load 预训练模型
    G_AB.load_state_dict(torch.load("saved_models/%s/G_AB_%d.pth" % (opt.dataset_name, opt.epoch)))
    G_BA.load_state_dict(torch.load("saved_models/%s/G_BA_%d.pth" % (opt.dataset_name, opt.epoch)))
    D_A.load_state_dict(torch.load("saved_models/%s/D_A_%d.pth" % (opt.dataset_name, opt.epoch)))
    D_B.load_state_dict(torch.load("saved_models/%s/D_B_%d.pth" % (opt.dataset_name, opt.epoch)))
else:
    # 否则初始化参数
    G_AB.apply(weights_init_normal)
    G_BA.apply(weights_init_normal)
    D_A.apply(weights_init_normal)
    D_B.apply(weights_init_normal)

# Optimizers
optimizer_G = torch.optim.Adam(
    itertools.chain(G_AB.parameters(), G_BA.parameters()), lr=opt.lr, betas=(opt.b1, opt.b2)
)
optimizer_D_A = torch.optim.Adam(D_A.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
optimizer_D_B = torch.optim.Adam(D_B.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))

# Learning rate update schedulers
lr_scheduler_G = torch.optim.lr_scheduler.LambdaLR(
    optimizer_G, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step
)
lr_scheduler_D_A = torch.optim.lr_scheduler.LambdaLR(
    optimizer_D_A, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step
)
lr_scheduler_D_B = torch.optim.lr_scheduler.LambdaLR(
    optimizer_D_B, lr_lambda=LambdaLR(opt.n_epochs, opt.epoch, opt.decay_epoch).step
)

Tensor = torch.cuda.FloatTensor if cuda else torch.Tensor

# Buffers of previously generated samples
fake_A_buffer = ReplayBuffer()
fake_B_buffer = ReplayBuffer()

# Image transformations
transforms_ = [
    transforms.Resize(int(opt.img_height * 1.12), Image.BICUBIC),
    transforms.RandomCrop((opt.img_height, opt.img_width)),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
]

# Training data loader
dataloader = DataLoader(
    ImageDataset("/data/shenliao/%s" % opt.dataset_name, transforms_=transforms_, unaligned=True),
    batch_size=opt.batch_size,
    shuffle=True,
    num_workers=opt.n_cpu,
)
# Test data loader
val_dataloader = DataLoader(
    ImageDataset("/data/shenliao/%s" % opt.dataset_name, transforms_=transforms_, unaligned=True, mode="test"),
    batch_size=5,
    shuffle=True,
    num_workers=1,
)
criterionSpatial = sc_model.SpatialCorrelativeLoss(opt.loss_mode, opt.patch_nums, opt.patch_size, opt.use_norm,opt.learned_attn, gpu_ids=0, T=opt.T).cuda()

def sample_images(batch,epoch):
    """Saves a generated sample from the test set"""
    imgs = next(iter(val_dataloader))
    G_AB.eval()
    G_BA.eval()
    real_A = Variable(imgs["A"].type(Tensor))
    fake_B = G_AB(real_A)
    real_B = Variable(imgs["B"].type(Tensor))
    fake_A = G_BA(real_B)
    cycle_A = G_BA(G_AB(real_A))
    cycle_B = G_AB(G_BA(real_B)) 
    # Arange images along x-axis
    real_A = make_grid(real_A, nrow=5, normalize=True)
    real_B = make_grid(real_B, nrow=5, normalize=True)
    fake_A = make_grid(fake_A, nrow=5, normalize=True)
    fake_B = make_grid(fake_B, nrow=5, normalize=True)
    cycle_A = make_grid(cycle_A, nrow=5, normalize=True)
    cycle_B = make_grid(cycle_B, nrow=5, normalize=True)
    # Arange images along y-axis
    image_grid = torch.cat((real_A, fake_B, cycle_A, real_B, fake_A, cycle_B), 1)
    save_image(image_grid, "images/%s/%s+%s.png" % (opt.dataset_name, epoch ,batch), normalize=False)

def Spatial_Loss(net, src, tgt, other=None):
        """给定源图像和目标图像来计算空间相似性和相异性损失"""
        attn_layers = [int(i) for i in opt.attn_layers.split(',')]
        n_layers = len(attn_layers)
        feats_src = net(src, attn_layers, encode_only=True)
        feats_tgt = net(tgt, attn_layers, encode_only=True)
        if other is not None:
            feats_oth = net(torch.flip(other, [2, 3]),attn_layers, encode_only=True)
        else:
            feats_oth = [None for _ in range(n_layers)]

        total_loss = 0.0
        for i, (feat_src, feat_tgt, feat_oth) in enumerate(zip(feats_src, feats_tgt, feats_oth)):
            loss = criterionSpatial.loss(feat_src, feat_tgt, feat_oth, i)
            total_loss += loss.mean()

        if not criterionSpatial.conv_init:
            criterionSpatial.update_init_()

        return total_loss / n_layers

# ----------
#  Training
# ----------

prev_time = time.time()
for epoch in range(opt.epoch, opt.n_epochs+1):
    G_loss=0
    D_loss=0
    GAN_loss=0
    cycle_loss=0
    identity_loss=0
    log = []
    for i, batch in enumerate(dataloader):

        # 真实图像
        real_A = Variable(batch["A"].type(Tensor))
        real_B = Variable(batch["B"].type(Tensor))

        # Adversarial ground truths
        valid = Variable(Tensor(np.ones((real_A.size(0), *D_A.output_shape))), requires_grad=False)
        fake = Variable(Tensor(np.zeros((real_A.size(0), *D_A.output_shape))), requires_grad=False)

        # ------------------
        #  Train Generators
        # ------------------

        G_AB.train()
        G_BA.train()

        optimizer_G.zero_grad()

        # Identity loss
        loss_id_A = criterion_identity(G_BA(real_A), real_A)
        loss_id_B = criterion_identity(G_AB(real_B), real_B)

        loss_identity = (loss_id_A + loss_id_B) / 2

        # GAN loss
        fake_B = G_AB(real_A)
        loss_GAN_AB = criterion_GAN(D_B(fake_B), valid)
        fake_A = G_BA(real_B)
        loss_GAN_BA = criterion_GAN(D_A(fake_A), valid)

        loss_GAN = (loss_GAN_AB + loss_GAN_BA) / 2
        
        # Cycle loss
        recov_A = G_BA(fake_B)
        loss_cycle_A = criterion_cycle(recov_A, real_A)
        recov_B = G_AB(fake_A)
        loss_cycle_B = criterion_cycle(recov_B, real_B)

        loss_cycle = (loss_cycle_A + loss_cycle_B) / 2
        
        emb_recov_B = encoder(recov_B)
        emb_imgs_A = encoder(real_A).detach()
        emb_recov_A = encoder(recov_A)
        emb_imgs_B = encoder(real_B).detach()
        spatial_loss = Spatial_Loss(netPre, real_A, fake_B, None)
        # Total loss
        if epoch < opt.warmup_epoches:
          loss_G = opt.lambda_cyc * loss_cycle + opt.lambda_id * loss_identity + opt.lambda_ct*content_loss(emb_recov_B,emb_imgs_B) +  opt.lambda_ct*content_loss(emb_recov_A,emb_imgs_A) + opt.lambda_sc*spatial_loss
        else:
          loss_G = opt.lambda_adv * loss_GAN + opt.lambda_cyc * loss_cycle + opt.lambda_id * loss_identity + opt.lambda_ct*content_loss(emb_recov_B,emb_imgs_B) +  opt.lambda_ct*content_loss(emb_recov_A,emb_imgs_A) + opt.lambda_sc*spatial_loss

        loss_G.backward()
        optimizer_G.step()

        # -----------------------
        #  Train Discriminator A
        # -----------------------

        optimizer_D_A.zero_grad()

        # Real loss
        loss_real = criterion_GAN(D_A(real_A), valid)
        # Fake loss (on batch of previously generated samples)
        fake_A_ = fake_A_buffer.push_and_pop(fake_A)
        loss_fake = criterion_GAN(D_A(fake_A_.detach()), fake)
        # Total loss
        loss_D_A = (loss_real + loss_fake) / 2

        loss_D_A.backward()
        optimizer_D_A.step()

        # -----------------------
        #  Train Discriminator B
        # -----------------------

        optimizer_D_B.zero_grad()

        # Real loss
        loss_real = criterion_GAN(D_B(real_B), valid)
        # Fake loss (on batch of previously generated samples)
        fake_B_ = fake_B_buffer.push_and_pop(fake_B)
        loss_fake = criterion_GAN(D_B(fake_B_.detach()), fake)
        # Total loss
        loss_D_B = (loss_real + loss_fake) / 2

        loss_D_B.backward()
        optimizer_D_B.step()

        loss_D = (loss_D_A + loss_D_B) / 2

        # --------------
        #  Log Progress
        # --------------

        # 确定大约剩余时间
        batches_done = epoch * len(dataloader) + i
        batches_left = opt.n_epochs * len(dataloader) - batches_done
        time_left = datetime.timedelta(seconds=batches_left * (time.time() - prev_time))
        prev_time = time.time()
        # Print log
        sys.stdout.write(
            "\r[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f, adv: %f, cycle: %f, identity: %f] ETA: %s"
            % (
                epoch,
                opt.n_epochs,
                i,
                len(dataloader),
                loss_D.item(),
                loss_G.item(),
                loss_GAN.item(),
                loss_cycle.item(),
                loss_identity.item(),
                time_left,
            )
        )
        log.append("\n[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f, adv: %f, cycle: %f, identity: %f] ETA: %s"
            % (
                epoch,
                opt.n_epochs,
                i,
                len(dataloader),
                loss_D.item(),
                loss_G.item(),
                loss_GAN.item(),
                loss_cycle.item(),
                loss_identity.item(),
                time_left,
            ))
        # 记录一个epoch的平均loss
        D_loss+=loss_D.item()/len(dataloader)
        G_loss+=loss_G.item()/len(dataloader)
        GAN_loss+=loss_GAN.item()/len(dataloader)
        cycle_loss+=loss_cycle.item()/len(dataloader)
        identity_loss+=loss_identity.item()/len(dataloader)
        # 满足采样间隔后，采样图像
        if batches_done % opt.sample_interval == 0:
            sample_images(i,epoch)
    
    # 更新学习率
    lr_scheduler_G.step()
    lr_scheduler_D_A.step()
    lr_scheduler_D_B.step()
    # 记录tensorboard
    # writer.add_scalar('D loss', D_loss, epoch)
    # writer.add_scalar('G loss', G_loss, epoch)
    # writer.add_scalar('GAN loss', GAN_loss, epoch)
    # writer.add_scalar('cycle loss', cycle_loss, epoch)
    # writer.add_scalar('identity loss', identity_loss, epoch)
    # 将loss写入loss.txt
    with open('./loss.txt', 'a+') as f:
            for i in range(len(log)):
                f.write(log[i])
    if opt.checkpoint_interval != -1 and epoch % opt.checkpoint_interval == 0:
        # 保存模型 checkpoints
        torch.save(G_AB.state_dict(), "saved_models/%s/G_AB_%d.pth" % (opt.dataset_name, epoch))
        torch.save(G_BA.state_dict(), "saved_models/%s/G_BA_%d.pth" % (opt.dataset_name, epoch))
        torch.save(D_A.state_dict(), "saved_models/%s/D_A_%d.pth" % (opt.dataset_name, epoch))
        torch.save(D_B.state_dict(), "saved_models/%s/D_B_%d.pth" % (opt.dataset_name, epoch))
# writer.close()