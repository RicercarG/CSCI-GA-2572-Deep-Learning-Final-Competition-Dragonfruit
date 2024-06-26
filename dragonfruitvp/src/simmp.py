import os

import torch
import torchmetrics

from torch import nn

from dragonfruitvp.src.base_method import Base_method
from dragonfruitvp.modules import (ConvSC, ConvNeXtSubBlock, ConvMixerSubBlock, GASubBlock, gInception_ST,
                                   HorNetSubBlock, MLPMixerSubBlock, MogaSubBlock, PoolFormerSubBlock,
                                   SwinSubBlock, UniformerSubBlock, VANSubBlock, ViTSubBlock, TAUSubBlock)

from dragonfruitvp.utils.metrics import metric
from dragonfruitvp.utils.vis import save_images, save_masks

class SimMP(Base_method):
    def __init__(self, eps=0, **kwargs):
        '''
        eps: controls how much extra weight is put on the last frame prediction, default to be 0
        ''' 
        super().__init__(**kwargs)
        self.eps = eps
        self.criterion = nn.CrossEntropyLoss()

        # for calculating average iou
        self.val_iou = []
        self.val_last_iou = []
        self.test_iou = []

        self.val_last_pred = None
        self.val_last_true = None

        self.val_first_pred = None
        self.val_first_true = None

        self.test_pred = None
        self.test_true = None
        
        # for competition submission
        self.submission = None

    def _build_model(self, **kwargs):
        return SimMP_Model(**kwargs)
    
    def forward(self, batch_x, batch_y=None, **kwargs):
        pre_seq_length, aft_seq_length = self.hparams.pre_seq_length, self.hparams.aft_seq_length
        if aft_seq_length == pre_seq_length:
            pred_y = self.model(batch_x)
        elif aft_seq_length < pre_seq_length:
            pred_y = self.model(batch_x)
            pred_y = pred_y[:, :aft_seq_length]
        elif aft_seq_length > pre_seq_length:
            pred_y = []
            d = aft_seq_length // pre_seq_length
            m = aft_seq_length % pre_seq_length
            
            cur_seq = batch_x.clone()
            for _ in range(d):
                cur_seq = self.model(cur_seq)
                pred_y.append(cur_seq)
            
            if m != 0:
                cur_seq = self.model(cur_seq)
                pred_y.append(cur_seq[:, :m])
            
            pred_y = torch.cat(pred_y, dim=1)
        return pred_y
    
    def training_step(self, batch, batch_idx):
        assert len(batch) == 3
        batch_x, _, batch_y = batch
        pred_y = self(batch_x)
        B, T, M, H, W = pred_y.shape
        true_y = batch_y[:, T:, :, :]
        # print('pred y', pred_y.shape, 'true y', true_y.shape)
        last_pred_y = pred_y[:, -1, :, :, :].squeeze(1)
        last_true_y = pred_y[:, -1, :, :].squeeze(1)
        # loss = self.eps*self.criterion(last_pred_y, last_true_y) + self.criterion(pred_y.reshape(B*T, M, H, W), true_y.reshape(B*T, H, W))
        loss = self.criterion(pred_y.reshape(B*T, M, H, W), true_y.reshape(B*T, H, W))
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        '''
        if dataset i labeled, we use it to train unet, the batch is [pre_seqs, aft_seqs, masks]
        if dataset is unlabeled, we use it to pretrain vp, batch is [pre_seqs, aft_seqs]
        hidden dataset will never be used for validation
        '''
        assert len(batch) == 3
        batch_x, _, batch_y = batch
        pred_y = self(batch_x, batch_y)
        B, T, M, H, W = pred_y.shape
        true_y = batch_y[:, T:, :, :]

        pred_y_last = pred_y[:,-1,:,:,:].squeeze(1)
        batch_y_last = true_y[:,-1,:,:].squeeze(1)

        pred_y_first = pred_y[:,0,:,:,:].squeeze(1)
        batch_y_first = true_y[:,0,:,:].squeeze(1)
        
        pred_y_cat = pred_y.reshape(B*T, M, H, W)
        true_y_cat = true_y.reshape(B*T, H, W)
        
        loss = self.criterion(pred_y_cat, true_y_cat)
        
        self.log('val_loss', loss, on_step=True, on_epoch=True, prog_bar=False)
        
        _, pred_y_mask = torch.max(pred_y_cat, 1)
        _, pred_y_last_mask = torch.max(pred_y_last, 1)
        _, pred_y_first_mask = torch.max(pred_y_first, 1)

        # move tensors to cpu
        pred_y_mask = pred_y_mask.cpu()
        true_y_cat = true_y_cat.cpu()
        pred_y_last_mask = pred_y_last_mask.cpu()
        batch_y_last = batch_y_last.cpu()
        pred_y_first_mask = pred_y_first_mask.cpu()
        batch_y_first = batch_y_first.cpu()

        # self.val_pred = torch.cat((self.val_pred, pred_y_mask), dim=0) if self.val_pred is not None else pred_y_mask
        # self.val_true = torch.cat((self.val_true, true_y_cat), dim=0) if self.val_true is not None else true_y_cat
    
        self.val_last_pred = torch.cat((self.val_last_pred, pred_y_last_mask), dim=0) if self.val_last_pred is not None else pred_y_last_mask
        self.val_last_true = torch.cat((self.val_last_true, batch_y_last), dim=0) if self.val_last_true is not None else batch_y_last

        self.val_first_pred = torch.cat((self.val_first_pred, pred_y_first_mask), dim=0) if self.val_first_pred is not None else pred_y_first_mask
        self.val_first_true = torch.cat((self.val_first_true, batch_y_first), dim=0) if self.val_first_true is not None else batch_y_first

        if self.vis_val:
            for b in range(pred_y.shape[0]):
                save_path = os.path.join('vis_mptrain', f'{batch_idx}_{b}')
                masks_pred = pred_y[b]
                _, masks_pred = torch.max(masks_pred, 1)
                masks_true = true_y[b]
                save_masks(masks_pred, save_path, name='pred')
                save_masks(masks_true, save_path, name='true')

        return loss

    def on_validation_epoch_end(self):
        jaccard = torchmetrics.JaccardIndex(task="multiclass", num_classes=49)
        # full_iou = jaccard(self.val_pred, self.val_true).item()
        last_iou = jaccard(self.val_last_pred, self.val_last_true).item()
        first_iou = jaccard(self.val_first_pred, self.val_first_true).item()
        print('validation last iou:', last_iou, 'validation first iou:', first_iou)
        
        self.val_last_pred = None
        self.val_last_true = None

        self.val_first_pred = None
        self.val_first_true = None

    def test_step(self, batch, batch_idx):
        if len(batch) == 3: # for testing iou
            batch_x, _, batch_y = batch
            pred_y = self(batch_x, batch_y)
            B, T, M, H, W = pred_y.shape
            true_y = batch_y[:, T:, :, :]

            last_pred_y = pred_y[:,-1,:,:,:].squeeze(1).cpu().detach()
            last_true_y = true_y[:,-1,:,:].squeeze(1).cpu()
            
            _, pred_tensor = torch.max(last_pred_y, 1)

            self.test_outputs.append(last_pred_y)

            self.test_pred = torch.cat((self.test_pred, pred_tensor), dim=0) if self.test_pred is not None else pred_tensor
            self.test_true = torch.cat((self.test_true, last_true_y), dim=0) if self.test_true is not None else last_true_y
   
            if self.vis_val:
                save_path = os.path.join('vis_test', self.ex_name, f'{batch_idx}_{B}')
                save_masks(pred_tensor, save_path, name='pred')
                save_masks(last_true_y, save_path, name='true')

        elif len(batch) == 1: # for competition submission, the hidden dataset only has video seq in
            pred_y = self(batch)
            last_pred_y = pred_y[:,-1,:,:,:].squeeze(1)
            _, mask_pred = torch.max(last_pred_y, 1)
            mask_pred = mask_pred.to('cpu')
            self.submission = torch.cat((self.submission, mask_pred), dim=0) if self.submission is not None else mask_pred


    def on_test_epoch_end(self):
        if self.test_pred is not None and self.test_true is not None:
            jaccard = torchmetrics.JaccardIndex(task="multiclass", num_classes=49)
            iou = jaccard(self.test_pred.cpu(), self.test_true).cpu().item()
            print('iou during test', iou)

        if self.submission is not None:
            print('submission shape', self.submission.shape)
            save_path = 'team_12.pt'
            torch.save(self.submission, save_path)
            print('submission saved to team_12.pt')

            self.submission = None

        # reset all values 
        self.test_pred = None
        self.test_true = None  

        # print('--check test outputs--', len(self.test_outputs))
        if len(self.test_outputs) > 0:
            out_tensor = torch.cat(self.test_outputs, dim=0)
            save_root_dir = './papervis'
            results_dir = os.path.join(save_root_dir, 'results')
            os.makedirs(results_dir, exist_ok=True)
            tensor_save_path = os.path.join(results_dir, f'{self.ex_name}.pt')
            torch.save(out_tensor, tensor_save_path)
            print(f'results saved to {tensor_save_path}')

class SimMP_Model(nn.Module):
    def __init__(self, in_shape, hid_S=16, hid_T=256, N_S=4, N_T=4, model_type='gSTA',
                 mlp_ratio=8., drop=0.0, drop_path=0.0, spatio_kernel_enc=3, 
                 spatio_kernel_dec=3, act_inplace=True, **kwargs):
        """
        N_S: for downsample, 1 / 2**(N_S/2); also for the number of convSC in encoder&decoder
        N_T: numder of mid nets
        """
    
        super().__init__()
        T, C, H, W = in_shape # T is pre_seq_length
        H, W = int(H / 2**(N_S/2)), int(W / 2**(N_S/2)) # downsample 1 / 2**(N_S/2)
        act_inplace = False
        self.enc = Encoder(C, hid_S, N_S, spatio_kernel_enc, act_inplace=act_inplace)
        self.dec = Decoder(hid_S, 49, N_S, spatio_kernel_dec, act_inplace=act_inplace)

        model_type = 'gsta' if model_type is None else model_type.lower()
        if model_type == 'incepu':
            self.hid = MidIncepNet(T*hid_S, hid_T, N_T)
        else:
            self.hid = MidMetaNet(T*hid_S, hid_T, N_T,
                input_resolution=(H, W), model_type=model_type,
                mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)

        
    def forward(self, x_raw, **kwargs):
        # print('x_raw shape', x_raw.shape)
        B, T, C, H, W = x_raw.shape
        x = x_raw.view(B*T, C, H, W)

        embed, skip = self.enc(x)
        _, C_, H_, W_ = embed.shape

        z = embed.view(B, T, C_, H_, W_)
        hid = self.hid(z)
        hid = hid.reshape(B*T, C_, H_, W_)

        Y = self.dec(hid, skip)
        Y = Y.reshape(B, T, 49, H, W)
        return Y


def sampling_generator(N, reverse=False):
    samplings = [False, True] * (N // 2)
    if reverse: return list(reversed(samplings[:N]))
    else: return samplings[:N]


class Encoder(nn.Module):
    def __init__(self, C_in, C_hid, N_S, spatio_kernel, act_inplace=True):
        super().__init__()
        samplings = sampling_generator(N_S)
        self.enc = nn.Sequential(
            ConvSC(C_in, C_hid, spatio_kernel, downsampling=samplings[0], 
                act_inplace=act_inplace),
            *[ConvSC(C_hid, C_hid, spatio_kernel, downsampling=s,
                act_inplace=act_inplace) for s in samplings[1:]]
        )

    def forward(self, x):
        enc1 = self.enc[0](x)
        latent = enc1
        for i in range(1, len(self.enc)):
            latent = self.enc[i](latent)
        return latent, enc1


class Decoder(nn.Module):
    def __init__(self, C_hid, C_out, N_S, spatio_kernel, act_inplace=True):
        super().__init__()
        samplings = sampling_generator(N_S, reverse=True)
        self.dec = nn.Sequential(
            *[ConvSC(C_hid, C_hid, spatio_kernel, upsampling=s,
                act_inplace=act_inplace) for s in samplings[:-1]],
            ConvSC(C_hid, C_hid, spatio_kernel, upsampling=samplings[-1],
                act_inplace=act_inplace)
        )
        self.readout = nn.Conv2d(C_hid, C_out, 1)

    def forward(self, hid, enc1=None):
        for i in range(0, len(self.dec)-1):
            hid = self.dec[i](hid)
        Y = self.dec[-1](hid + enc1)
        Y = self.readout(Y)
        return Y


class MidIncepNet(nn.Module): # a unet
    def __init__(self, channel_in, channel_hid, N2, incep_ker=[3,5,7,11], groups=8, **kwargs):
        super().__init__()
        assert N2 >= 2 and len(incep_ker) > 1
        self.N2 = N2
        enc_layers = [gInception_ST(
            channel_in, channel_hid//2, channel_hid, incep_ker=incep_ker, groups=groups)]
        for i in range(1, N2-1):
            enc_layers.append(
                gInception_ST(channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))

        enc_layers.append(
                gInception_ST(channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))
        
        dec_layers = [gInception_ST(
            channel_hid, channel_hid//2, channel_hid, incep_ker=incep_ker, groups=groups)]
        
        for i in range(1, N2-1):
            dec_layers.append(
                gInception_ST(2*channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))
            
            dec_layers.append(
                gInception_ST(2*channel_hid, channel_hid//2, channel_in,
                              incep_ker=incep_ker, groups=groups))

        self.enc = nn.Sequential(*enc_layers)
        self.dec = nn.Sequential(*dec_layers)

    def forward(self, x):
        B, T, C, H, W = x.shape
        x = x.reshape(B, T*C, H, W)

        # encoder
        skips = []
        z = x
        for i in range(self.N2):
            z = self.enc[i](z)
            if i < self.N2-1:
                skips.append(z)
            
        # decoder
        z = self.dec[0](z)
        for i in range(1, self.N2):
            z = self.dec[i](torch.cat([z, skips[-i]], dim=1))
        
        y = z.reshape(B, T, C, H, W)
        return y

class MetaBlock(nn.Module):
    """The hidden Translator of MetaFormer for SimVP"""

    def __init__(self, in_channels, out_channels, input_resolution=None, model_type=None,
                 mlp_ratio=8., drop=0.0, drop_path=0.0, layer_i=0):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        model_type = model_type.lower() if model_type is not None else 'gsta'

        if model_type == 'gsta':
            self.block = GASubBlock(
                in_channels, kernel_size=21, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        elif model_type == 'convmixer':
            self.block = ConvMixerSubBlock(in_channels, kernel_size=11, activation=nn.GELU)
        elif model_type == 'convnext':
            self.block = ConvNeXtSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'hornet':
            self.block = HorNetSubBlock(in_channels, mlp_ratio=mlp_ratio, drop_path=drop_path)
        elif model_type in ['mlp', 'mlpmixer']:
            self.block = MLPMixerSubBlock(
                in_channels, input_resolution, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type in ['moga', 'moganet']:
            self.block = MogaSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop_rate=drop, drop_path_rate=drop_path)
        elif model_type == 'poolformer':
            self.block = PoolFormerSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'swin':
            self.block = SwinSubBlock(
                in_channels, input_resolution, layer_i=layer_i, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path)
        elif model_type == 'uniformer':
            block_type = 'MHSA' if in_channels == out_channels and layer_i > 0 else 'Conv'
            self.block = UniformerSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop,
                drop_path=drop_path, block_type=block_type)
        elif model_type == 'van':
            self.block = VANSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        elif model_type == 'vit':
            self.block = ViTSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'tau':
            self.block = TAUSubBlock(
                in_channels, kernel_size=21, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        else:
            assert False and "Invalid model_type in SimVP"

        if in_channels != out_channels:
            self.reduction = nn.Conv2d(
                in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        z = self.block(x)
        return z if self.in_channels == self.out_channels else self.reduction(z)


class MidMetaNet(nn.Module):
    """The hidden Translator of MetaFormer for SimVP"""

    def __init__(self, channel_in, channel_hid, N2,
                 input_resolution=None, model_type=None,
                 mlp_ratio=4., drop=0.0, drop_path=0.1):
        super().__init__()
        assert N2 >= 2 and mlp_ratio > 1
        self.N2 = N2
        dpr = [  # stochastic depth decay rule
            x.item() for x in torch.linspace(1e-2, drop_path, self.N2)]

        # downsample
        enc_layers = [MetaBlock(
            channel_in, channel_hid, input_resolution, model_type,
            mlp_ratio, drop, drop_path=dpr[0], layer_i=0)]
        # middle layers
        for i in range(1, N2-1):
            enc_layers.append(MetaBlock(
                channel_hid, channel_hid, input_resolution, model_type,
                mlp_ratio, drop, drop_path=dpr[i], layer_i=i))
        # upsample
        enc_layers.append(MetaBlock(
            channel_hid, channel_in, input_resolution, model_type,
            mlp_ratio, drop, drop_path=drop_path, layer_i=N2-1))
        self.enc = nn.Sequential(*enc_layers)

    def forward(self, x):
        B, T, C, H, W = x.shape
        x = x.reshape(B, T*C, H, W)

        z = x
        for i in range(self.N2):
            z = self.enc[i](z)

        y = z.reshape(B, T, C, H, W)
        return y
