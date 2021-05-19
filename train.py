import argparse
import glob
import os
import random
import time
from multiprocessing import cpu_count
from kfac import KFAC

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision.utils import save_image

from core import save, addvalue
from loss import DiceLoss, FocalLoss
from unet import UNet
# from utils.dataset import MulticlassCrackDataset as Dataset
from utils.dataset import LinerCrackDataset
from utils.util import miouf, prmaper,mAP
from kfacopitm import KFACOptimizer
from utils.cutmix import cutmix


def setcolor(idxtendor, colors):
    assert idxtendor.max() + 1 <= len(colors)
    B, H, W = idxtendor.shape
    colimg = torch.zeros(B, 3, H, W).to(idxtendor.device).to(idxtendor.device)
    colors = colors[1:]
    for b in range(B):
        for idx, color in enumerate(colors, 1):
            colimg[b, :, idxtendor[b] == idx] = (color.reshape(3, 1)).to(idxtendor.device).float()
    return colimg


def main(args):
    device = torch.device("cpu" if not torch.cuda.is_available() else args.device)
    print(device)

    masks = glob.glob(f'{args.maskfolder}/*.jpg')
    k_shot = int(len(masks) * 0.8) if args.k_shot == 0 else args.k_shot
    random.seed(0)
    trainmask = random.sample(masks, k=k_shot)
    validmask = sorted(list(set(masks) - set(trainmask)))
    import hashlib
    print(hashlib.md5("".join(validmask).encode()).hexdigest())
    unet = UNet(in_channels=3, out_channels=3,deconv=not args.upconv)
    kfac=KFAC(unet,0.1,update_freq=100)
    print(f'num_kfac_optimization:{len(list(kfac.params))},enable:{args.kfac}')
    if args.trainedmodel is not None:
        unet.load_state_dict(torch.load(args.trainedmodel))
    writer = {}
    worter = {}
    preepoch = 0
    unet.to(device)
    traindataset = LinerCrackDataset(f'{args.linerimgfolder}/train.txt', (args.size, args.size))
    validdataset = LinerCrackDataset(f'{args.linerimgfolder}/val.txt', (args.size, args.size))
    print(f'train"{len(traindataset)},val:{len(validdataset)}')
    trainloader = torch.utils.data.DataLoader(traindataset, batch_size=args.batchsize // args.subdivisions,
                                              shuffle=True,
                                              num_workers=args.workers)
    validloader = torch.utils.data.DataLoader(validdataset, batch_size=args.batchsize // args.subdivisions,
                                              shuffle=True,
                                              num_workers=args.workers)
    loaders = {'train': trainloader, 'valid': validloader}
    if args.saveimg: unet.savefolder = args.savefolder
    if args.loss == 'DSC':
        lossf = DiceLoss()
    elif args.loss == 'CE':
        lossf = nn.CrossEntropyLoss()
    elif args.loss == 'Focal':
        lossf = FocalLoss()
    else:
        assert False, 'set correct loss.'

    if args.optimizer=='Adam':
        optimizer = optim.Adam(unet.parameters(), lr=args.lr)
    elif args.optimizer=='SGD':
        optimizer=optim.SGD(unet.parameters(),lr=1e-3,momentum=0.9,dampening=1e-3,weight_decay=0)
    elif args.optimizer=='KFAC':
        optimizer=KFACOptimizer(unet)
        optimizer.acc_stats=True
    else:
        assert False,'set correct optimizer'
    clscolor = torch.tensor([[0, 0, 0], [255, 255, 255], [0, 255, 0]])

    epochtime = {}
    os.makedirs(args.savefolder, exist_ok=True)
    for epoch in range(preepoch, args.epochs):
        # args.num_train=0
        for phase in ["train"] * args.num_train + ["valid"]:
            # for phase in ['valid']:
            valid_miou = []
            losslist = []
            map=[]
            prmap = torch.zeros(3, 3)

            if phase == "train":
                print('start train')
                unet.train()
                if args.resize:
                    traindataset.resize()
            else:
                unet.eval()
            batchstarttime = 0
            epochstarttime=time.time()
            for batchidx, data in enumerate(loaders[phase]):
                print(f'batchtime:{time.time() - batchstarttime}')
                batchstarttime = time.time()
                x, y_true = data
                x, y_true = x.to(device), y_true.to(device).float()
                with torch.set_grad_enabled(phase == "train"):
                    if args.mixup and phase == 'train':
                        if args.alpha > 0:
                            lam = np.random.beta(args.alpha, args.alpha)
                        else:
                            lam = 1
                        rndidx = np.random.permutation(range(x.size[0]))
                        x = lam * x + (1 - lam) * x[rndidx]
                        # ToPILImage()(x[0].detach().cpu()).show()
                        # exit()
                        y_pred = unet(x)
                        loss = lam * lossf(y_pred, y_true) + (1 - lam) * lossf(y_pred, y_true[rndidx])
                    else:
                        y_pred = unet(x)
                        loss = lossf(y_pred, y_true.long())
                        if args.CR:
                            x_cutmix,cutparam=cutmix(x)
                            y_pred_cutmix,_=cutmix(y_pred,cutparam)
                            CRloss=F.mse_loss(unet(x_cutmix),y_pred_cutmix)
                            print(CRloss)
                            addvalue(writer, f'CRloss:{phase}', CRloss.item(), epoch)
                    losslist += [loss.item()]
                    print(f'{epoch} {batchidx}/{len(loaders[phase])} {loss.item():.6f},{phase}')
                    print(f'time:{time.time() - batchstarttime}')
                    if phase == "train":
                        # y_pred.retain_grad()
                        (loss / args.subdivisions).backward()
                        # gradlist=cal_grad_ratio(y_pred,y_true).numpy()
                        # for i in range(3):
                        #     addvalue(writer,f'grad:{i}',gradlist[i],epoch)
                        # print(gradlist)
                        if (batchidx + 1) % args.subdivisions == 0:
                            print('step')
                            if args.kfac:
                                kfac.step()
                            optimizer.step()
                            optimizer.zero_grad()

                    miou = miouf(y_pred, y_true).item()
                    valid_miou += [miou]
                    prmap += prmaper(y_pred, y_true, 3)
                    map+=[mAP(y_pred,y_true)]
                    if batchidx == 0: save_image(
                        torch.cat([x, setcolor(y_true, clscolor), setcolor(y_pred.argmax(1), clscolor)], dim=2),
                        f'{args.savefolder}/{epoch}.jpg')
            epochtime[phase]=time.time()-epochstarttime
            print(epochtime)
            addvalue(writer, f'loss:{phase}', np.mean(losslist), epoch)
            addvalue(writer, f'mIoU:{phase}', np.nanmean(valid_miou), epoch)
            addvalue(writer, f'mAP:{phase}', np.nanmean(map), epoch)
            print(f'{epoch=}/{args.epochs}:{phase}:{np.mean(losslist):.4f},miou:{np.nanmean(valid_miou):.4f},mAP"{np.nanmean(map):.4f}')
            print((prmap / ((batchidx + 1) * args.batchsize)).int())
        save(unet, args.savefolder, writer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Training U-Net model for segmentation of brain MRI"
    )
    parser.add_argument(
        "--batchsize",
        type=int,
        default=32,
        help="input batch size for training (default: 8)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=100,
        help="number of epochs to train (default: 100)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1e-3,
        help="initial learning rate (default: 0.001)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help="device for training (default: cuda)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help="number of workers for data loading (default: max)",
    )
    parser.add_argument(
        "--pretrained",
        default=False,
        action='store_true'
    )
    parser.add_argument(
        "--k-shot",
        default=0,
        type=int
    )
    parser.add_argument(
        "--num-train",
        default=1,
        type=int
    )
    parser.add_argument(
        "--cutpath",
        default=False,
        action='store_true'
    )
    parser.add_argument(
        "--savefolder",
        default='tmp',
        type=str
    )
    parser.add_argument(
        "--rawfolder",
        default='../data/owncrack/scene/image',
        type=str
    )
    parser.add_argument(
        "--maskfolder",
        default='../data/owncrack/scene/mask',
        type=str
    )
    parser.add_argument(
        '--loss',
        default='CE',
    )
    parser.add_argument(
        '--split',
        type=int,
        default=1
    )
    parser.add_argument(
        '--random',
        default=False,
        action='store_true'
    )
    parser.add_argument(
        '--saveimg',
        default=False,
        action='store_true'
    )
    parser.add_argument(
        '--resume',
        default=False,
        action='store_true'
    )
    parser.add_argument(
        '--resize',
        default=False,
        action='store_true'
    )
    parser.add_argument(
        '--jitter',
        default=0,
        type=int
    )
    parser.add_argument(
        '--jitter_block',
        default=1,
        type=int
    )
    parser.add_argument(
        '--subdivisions',
        default=1,
        type=int
    )
    parser.add_argument(
        '--elastic',
        default=False,
        action='store_true'
    )
    parser.add_argument(
        '--dropout',
        default=0,
        type=float
    )
    parser.add_argument(
        '--size',
        default=256,
        type=int,
    )
    parser.add_argument(
        '--linerimgfolder',
        default='datasets/liner'
    )
    parser.add_argument(
        '--trainedmodel',
        default=None
    )
    parser.add_argument('--kfac',default=False,action='store_true')
    parser.add_argument('--mixup', default=False, action='store_true')
    parser.add_argument('--CR', default=False, action='store_true')
    parser.add_argument('--alpha', default=1, type=float)
    parser.add_argument('--half', default=False, action='store_true')
    parser.add_argument('--upconv',default=False,action='store_true')
    parser.add_argument('--optimizer',default='Adam')
    args = parser.parse_args()
    args.num_train = args.split
    args.epochs *= args.split
    args.savefolder = f'data/{args.savefolder}'
    main(args)
