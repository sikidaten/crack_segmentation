from utils.dataset import MulticlassCrackDataset as Dataset
import numpy as np
from core import load
import torch
from train import setcolor
from unet import UNet
from utils.util import prmaper,miouf
import os
device='cuda'
unet1 = UNet(in_channels=3, out_channels=3).to(device)
savefolder='data/crack_report/split2focal_x/'
folder='data/out/split2focal_x'
split=2

os.makedirs(folder,exist_ok=True)
saved = load(savefolder)
writer, preepoch, modelpath, worter = saved['writer'], saved['epoch'], saved['modelpath'], saved['worter']
trainmask, validmask = worter['trainmask'], worter['validmask']
unet1.load_state_dict(torch.load(modelpath))
print('load model')

validdataset = Dataset(validmask, train=False, random=False, split=split)
validdataset.ret_item=True
prmap1=np.zeros((split,split,3,3))
ioul=[[[] for _ in range(split)] for __ in range(split)]
meaniou=[]
unet1.eval()
saveoutput=[]
saveimg=[]
_ious=[]
with torch.set_grad_enabled(False):
    for idx,(img,target,(item,pos)) in enumerate(validdataset):
        print(idx)
        img=img.to(device).unsqueeze(0)
        target=target.to(device).unsqueeze(0)
        output1=unet1(img)
        corx,cory,_=pos
        prmap1[corx,cory]+=prmaper(output1,target,3).detach().cpu().numpy()
        miou1=miouf(output1,target,3)
        _ious+=[miou1.item()]
        print(miou1)
        print(np.nanmean(_ious))
        print(pos)
        from torchvision.utils import save_image
        from torchvision.transforms import ToPILImage
        if split==1:
            save_image(img,f'{folder}/{idx}_img.png')
            save_image(setcolor(output1.argmax(1),validdataset.clscolor), f'{folder}/{idx}_out1.png')
        elif split==2:
            saveoutput+=[output1]
            saveimg+=[img]
            if (pos[0]==1) and (pos[1]==1):
                catmask=torch.cat([torch.cat([saveoutput[0], saveoutput[1]], dim=2), torch.cat([saveoutput[2], saveoutput[3]], dim=2)], dim=3)
                save_image(setcolor(catmask.argmax(1),validdataset.clscolor),f'{folder}/{idx//4}_out.png')
                catimg=torch.cat([torch.cat([saveimg[0], saveimg[1]], dim=2), torch.cat([saveimg[2], saveimg[3]], dim=2)], dim=3)
                save_image(catimg,f'{folder}/{idx//4}.png')

                saveimg=[]
                saveoutput=[]
