import torch
import torch.nn.functional as F
import torch.nn as nn
import math

from torch import randperm as perm
from torch import cat

    
class HeadSegment(nn.Module):
    def __init__(self, dim, reduced_dim):
        super().__init__()
        self.dim = dim
        self.reduced_dim = reduced_dim

        self.f1 = nn.Conv2d(self.dim, self.reduced_dim, (1, 1))
        self.f2 = nn.Sequential(nn.Conv2d(self.dim, self.dim, (1, 1)),
                                nn.ReLU(),
                                nn.Conv2d(self.dim, self.reduced_dim, (1, 1)))
        
    def forward(self, feat, drop=nn.Identity()):
        feat = Segment_DETR.transform(feat)
        feat = self.f1(drop(feat)) + self.f2(drop(feat))
        return Segment_DETR.untransform(feat)
    
class ProjectionSegment(nn.Module):
    def __init__(self, func):
        super().__init__()
        self.f = func
        
    def forward(self, feat, drop=nn.Identity()):
        feat = Segment_DETR.transform(feat)
        feat = self.f(drop(feat))
        return Segment_DETR.untransform(feat)
        

class DETR(nn.Module):
    def __init__(self, dim, reduced_dim, num_queries, nhead=1, dropout=0.1):
        super().__init__()
        self.pos_embed = nn.Parameter(torch.randn(num_queries, dim))
        
        self.self_attn = nn.MultiheadAttention(dim, nhead, dropout=dropout, batch_first=True)
        self.cross_attn = nn.MultiheadAttention(dim, nhead, dropout=dropout, batch_first=True)

        self.linear1 = nn.Conv2d(dim, reduced_dim, kernel_size=1)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Conv2d(reduced_dim, dim, kernel_size=1)

        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)


        self.ffn = HeadSegment(dim, reduced_dim)

    def forward(self, tgt, memory, drop):
        pos_embed = self.pos_embed.unsqueeze(0)
        tgt2 = self.self_attn(tgt + pos_embed, 
                              tgt + pos_embed, 
                              value=tgt)[0]
        tgt = tgt + self.dropout1(tgt2)
        tgt = self.norm1(tgt)
        tgt2 = self.cross_attn(query=tgt + pos_embed, 
                               key=memory, 
                               value=memory)[0]
        tgt = tgt + self.dropout2(tgt2)
        tgt = self.norm2(tgt)
        tgt = Segment_DETR.transform(tgt)
        tgt2 = self.linear2(self.dropout(F.relu(self.linear1(tgt))))
        tgt = tgt + self.dropout3(tgt2)
        tgt = Segment_DETR.untransform(tgt)
        tgt = memory + self.norm3(tgt)
        return self.ffn(tgt, drop)

class Decoder(nn.Module):
    def __init__(self, args, codebook):
        super().__init__()
        self.codebook = codebook

        # DETR decoder
        self.decoder = DETR(args.dim, args.reduced_dim, args.num_queries)

    def forward(self, feat, drop=nn.Identity()):        
        discrete_query = Segment_DETR.vqt(feat, self.codebook)
        dec_feat = self.decoder(discrete_query, feat, drop)
        return dec_feat
    

class Segment_DETR(nn.Module):
    def __init__(self, args):
        super().__init__()

        ##################################################################################
        # [Configuration]
        # argument
        self.args = args

        # dimension
        self.dim = args.dim
        self.reduced_dim = args.reduced_dim
        self.projection_dim = args.projection_dim

        # number of cluster
        self.num_codebook = args.num_codebook
        ##################################################################################

        ##################################################################################
        # [Codebook]
        self.codebook = nn.Parameter(torch.empty(args.num_codebook, self.dim))
        self.reset(self.codebook, args.num_codebook)
        ##################################################################################
        
        ##################################################################################
        # DETR Decoder Head  
        self.head = Decoder(args, self.codebook)
        self.head_ema = Decoder(args, self.codebook)

        # dropout
        self.dropout = torch.nn.Dropout(p=0.1)
        ##################################################################################
        

        ##################################################################################
        # For Effective contrastive with EMA
        # projection head
        self.projection_head = ProjectionSegment(nn.Conv2d(self.reduced_dim, self.projection_dim, kernel_size=1))
        self.projection_head_ema = ProjectionSegment(nn.Conv2d(self.reduced_dim, self.projection_dim, kernel_size=1))
        ##################################################################################


        ##################################################################################
        # [Probe]
        # linear_probe
        self.linear_probe = nn.Conv2d(self.reduced_dim, args.n_classes, (1, 1))

        # cluster centroid
        self.cluster_probe = torch.nn.Parameter(torch.randn(args.n_classes, self.reduced_dim))
        self.reset(self.cluster_probe, args.n_classes)
        ##################################################################################        

    @property
    def num_param(self):
        out = 0
        for param in self.head.parameters():
            out += param.numel()
        return out

    @staticmethod
    def quantize_index(z, c, mode='cos'):
        if mode == 'cos':
            # computing distance
            dist = Segment_DETR.cos_distance_matrix(z, c)
        elif mode == 'l2':
            dist = Segment_DETR.l2_distance_matrix(z, c)

        # quantize
        return dist.argmax(dim=2)

    @staticmethod
    def cos_distance_matrix(z, c):
        # flatten z
        z_flattened = z.contiguous().view(-1, z.shape[-1])
        norm_z = F.normalize(z_flattened, dim=1)
        norm_embed = F.normalize(c, dim=1)
        return torch.einsum("ab,cb->ac", norm_z, norm_embed).view(*z.shape[:-1], -1)

    @staticmethod
    def l2_distance_matrix(z, c):
        # flatten z
        z_flattened = z.contiguous().view(-1, z.shape[-1])
        dist = (z_flattened.square().sum(dim=1, keepdims=True) + c.square().sum(dim=1).unsqueeze(0)
        -2 * z_flattened @ c.transpose(0, 1)) / c.shape[1]
        return torch.exp(-dist/z.shape[2]/2).view(*z.shape[:-1], -1)

    @staticmethod
    def codebook_index(z, c):
        # computing distance
        dist = Segment_DETR.cos_distance_matrix(z, c)

        # codebook index
        return dist.argmax(dim=2)

    @staticmethod
    def vqt(z, c):
        """
        Return Vector-Quantized Tensor
        """
        codebook_ind = Segment_DETR.codebook_index(z, c)
        return c[codebook_ind].view(*z.shape[:-1], c.shape[1])

    def bank_init(self):
        self.prime_bank = {}
        start_of_tensor = torch.empty([0, self.projection_dim]).cuda()
        for i in range(self.num_codebook):
            self.prime_bank[i] = start_of_tensor

    def bank_update(self, feat, proj_feat_ema, max_num=100):
        # load all and bank collection
        quant_ind = self.quantize_index(feat, self.codebook)
        for i in quant_ind.unique():
            # key bank
            key = proj_feat_ema[torch.where(quant_ind == i)]

            # 50% random cutting
            key = key[perm(len(key))][:int(len(key)*0.5)]

            # merging
            self.prime_bank[i.item()] = cat([self.prime_bank[i.item()], key], dim=0)

            # bank length
            length = len(self.prime_bank[i.item()])

            # if maximum number is over, slice by the order of the older
            if length >= max_num:
                self.prime_bank[i.item()] = self.prime_bank[i.item()][length-max_num:]

    def bank_compute(self):
        bank_vq_feat = torch.empty([0, self.dim]).cuda()
        bank_proj_feat_ema = torch.empty([0, self.projection_dim]).cuda()
        for key in self.prime_bank.keys():
            num = self.prime_bank[key].shape[0]
            if num == 0: continue
            bank_vq_feat = cat([bank_vq_feat, self.codebook[key].unsqueeze(0).repeat(num, 1)], dim=0)
            bank_proj_feat_ema = cat([bank_proj_feat_ema, self.prime_bank[key]], dim=0)

        # normalized feature and flat its feature for computing correspondence
        self.flat_norm_bank_vq_feat = F.normalize(bank_vq_feat, dim=1)
        self.flat_norm_bank_proj_feat_ema = F.normalize(bank_proj_feat_ema, dim=1)


    def contrastive_ema_with_codebook_bank(self, feat, proj_feat, proj_feat_ema, temp=0.07, pos_thresh=0.3, neg_thresh=0.1):
        """
        get all anchors and positive samples with same codebook index
        """

        # quantized feature to positive sample and negative sample
        vq_feat = self.vqt(feat, self.codebook)
        norm_vq_feat = F.normalize(vq_feat, dim=2)
        flat_norm_vq_feat = self.flatten(norm_vq_feat)

        # normalized feature and flat its feature for computing correspondence
        norm_proj_feat = F.normalize(proj_feat, dim=2)

        # normalized feature and flat its feature for computing correspondence
        norm_proj_feat_ema = F.normalize(proj_feat_ema, dim=2)
        flat_norm_proj_feat_ema = self.flatten(norm_proj_feat_ema)

        # selecting anchors by one-batch for all correspondence to all-batches
        # positive/negative
        loss_NCE_list = []
        for batch_ind in range(proj_feat.shape[0]):

            # anchor selection
            anchor_vq_feat = norm_vq_feat[batch_ind]
            anchor_proj_feat = norm_proj_feat[batch_ind]

            # cosine similarity of student-teacher
            cs_st = anchor_proj_feat @ flat_norm_proj_feat_ema.T

            # Codebook distance
            codebook_distance = anchor_vq_feat @ flat_norm_vq_feat.T
            bank_codebook_distance = anchor_vq_feat @ self.flat_norm_bank_vq_feat.T

            # [1] student-teacher (in-batch, local)
            pos_mask = (codebook_distance > pos_thresh)
            neg_mask = (codebook_distance < neg_thresh)

            auto_mask = torch.ones_like(pos_mask)
            auto_mask[:, batch_ind * pos_mask.shape[0]:(batch_ind + 1) * pos_mask.shape[0]].fill_diagonal_(0)
            pos_mask *= auto_mask

            cs_teacher = cs_st / temp
            shifted_cs_teacher = cs_teacher - cs_teacher.max(dim=1, keepdim=True)[0].detach()
            shifted_cs_teacher_with_only_neg = shifted_cs_teacher.exp() * (pos_mask + neg_mask)
            pos_neg_loss_matrix_teacher = -shifted_cs_teacher + torch.log(shifted_cs_teacher_with_only_neg.sum(dim=1, keepdim=True))
            loss_NCE_list.append(pos_neg_loss_matrix_teacher[torch.where(pos_mask!=0)].mean())

            # [2] student-teacher bank (out-batch, global)
            if self.flat_norm_bank_proj_feat_ema.shape[0] != 0:

                # cosine similarity of student-teacher bank
                cs_st_bank = anchor_proj_feat @ self.flat_norm_bank_proj_feat_ema.T

                bank_pos_mask = (bank_codebook_distance > pos_thresh)
                bank_neg_mask = (bank_codebook_distance < neg_thresh)

                cs_teacher_bank = cs_st_bank / temp
                shifted_cs_teacher_bank = cs_teacher_bank - cs_teacher_bank.max(dim=1, keepdim=True)[0].detach()
                shifted_cs_teacher_bank_with_only_neg = shifted_cs_teacher_bank.exp() * (bank_pos_mask + bank_neg_mask)
                pos_neg_loss_matrix_teacher_bank = -shifted_cs_teacher_bank + torch.log(shifted_cs_teacher_bank_with_only_neg.sum(dim=1, keepdim=True))

                # loss append
                loss_NCE_list.append(pos_neg_loss_matrix_teacher_bank[torch.where(bank_pos_mask!=0)].mean())

        # front
        loss_front = sum(loss_NCE_list) / float(len(loss_NCE_list))
        return loss_front
    
    @staticmethod
    def auto_cs(x):
        a = F.normalize(x, dim=1)
        return a @ a.T

    @staticmethod
    def reset(x, n_c): x.data.uniform_(-1.0 / n_c, 1.0 / n_c)

    @staticmethod
    def ema_init(x, x_ema):
        for param, param_ema in zip(x.parameters(), x_ema.parameters()): param_ema.data = param.data; param_ema.requires_grad = False

    @staticmethod
    def ema_update(x, x_ema, lamb=0.99):
        for student_params, teacher_params in zip(x.parameters(), x_ema.parameters()):
            teacher_params.data = lamb * teacher_params.data + (1-lamb) * student_params.data


    @staticmethod
    def img_to_patch_for_affinity(img, patch_size):
        img_patch = img.unfold(2, patch_size, patch_size).unfold(3, patch_size, patch_size)
        img_patch = img_patch.permute(0, 2, 3, 1, 4, 5)
        img_patch = img_patch.reshape(*img_patch.shape[:3], -1)
        img_patch = img_patch.reshape(img_patch.shape[0], -1, img_patch.shape[3])
        return img_patch

    @staticmethod
    def get_modularity_matrix_and_edge(x, mode='cos'):
        """
            getting W=(A-ddT/2m) and getting all edges (e)
        """
        if mode=='cos':
            norm = F.normalize(x, dim=2)
            A = (norm @ norm.transpose(2, 1)).clamp(0)
        elif mode=='l2':
            A = Segment_DETR.compute_self_distance_batch(x)

        A = A - A * torch.eye(A.shape[1]).cuda()
        d = A.sum(dim=2, keepdims=True)
        e = A.sum(dim=(1, 2), keepdims=True)
        W = A - (d / e) @ (d.transpose(2, 1) / e) * e
        return W, e

    @staticmethod
    def cluster_assignment_matrix(z, c):
        norm_z = F.normalize(z, dim=2)
        norm_c = F.normalize(c, dim=1)
        return (norm_z @ norm_c.unsqueeze(0).transpose(2, 1)).clamp(0)

    @staticmethod
    def compute_modularity_based_codebook(c, x, temp=0.1, grid=False):

        # detach
        x = x.detach()

        # pooling for reducing GPU memory allocation
        if grid: x, _ = Segment_DETR.stochastic_sampling(x)

        # modularity matrix and its edge matrix
        W, e = Segment_DETR.get_modularity_matrix_and_edge(x)

        # cluster assignment matrix
        C = Segment_DETR.cluster_assignment_matrix(x, c)

        # tanh with temperature
        D = C.transpose(2, 1)
        E = torch.tanh(D.unsqueeze(3) @ D.unsqueeze(2) / temp)
        delta, _ = E.max(dim=1)
        Q = (W / e) @ delta

        # trace
        diag = Q.diagonal(offset=0, dim1=-2, dim2=-1)
        trace = diag.sum(dim=-1)

        return -trace.mean()

    @staticmethod
    def compute_self_distance_batch(x):
        dist = x.square().sum(dim=2, keepdims=True) + x.square().sum(dim=2).unsqueeze(1) -2 * (x @ x.transpose(2, 1))
        return torch.exp(-dist/x.shape[2])

    @staticmethod
    def img_to_patch(img, patch_size=16):
        img_patch = img.unfold(2, patch_size, patch_size).unfold(3, patch_size, patch_size)
        img_patch = img_patch.permute(0, 2, 3, 1, 4, 5)
        return img_patch.reshape(-1, *img_patch.shape[3:])

    @staticmethod
    def patch_to_img(patch, batch_size=16, patch_size=16, img_size=320):
        patch_ = patch.reshape(batch_size, img_size//patch_size, img_size//patch_size, 3, patch_size, patch_size)
        patch_ = patch_.permute(0, 3, 1, 4, 2, 5)
        return patch_.reshape(batch_size, 3, img_size, img_size)

    @staticmethod
    def transform(x):
        """
        B, P, D => B, D, root(P), root(P)

        Ex) 128, 400, 768 => 128, 768, 20, 20
        """
        B, P, D = x.shape
        return x.permute(0, 2, 1).view(B, D, int(math.sqrt(P)), int(math.sqrt(P)))

    @staticmethod
    def untransform(x):
        """
        B, D, P, P => B, P*P, D,

        Ex) 128, 768, 20, 20 => 128, 400, 768
        """
        B, D, P, P = x.shape
        return x.view(B, D, -1).permute(0, 2, 1)

    @staticmethod
    def flatten(x):
        """
        B, P, D => B*P, D

        Ex) 16, 400, 768 => 6400, 768
        """
        B, P, D = x.shape
        return x.contiguous().view(B*P, D)

    @staticmethod
    def unflatten(x, batch_size=16):
        """
        B*P, D => B, P, D

        Ex) 6400, 768 => 16, 400, 768
        """
        P, D = x.shape
        return x.contiguous().view(batch_size, P//batch_size, D)

    def linear(self, z):
        z = self.transform(z)
        return self.linear_probe(z)

    @staticmethod
    def stochastic_sampling(x, order=None, k=2):
        """
        pooling
        """
        x = Segment_DETR.transform(x)
        x_patch = x.unfold(2, k, k).unfold(3, k, k)
        x_patch = x_patch.permute(0, 2, 3, 4, 5, 1)
        x_patch = x_patch.reshape(-1, x_patch.shape[3:5].numel(), x_patch.shape[5])

        if order==None: order = torch.randint(k ** 2, size=(x_patch.shape[0],))

        x_patch = x_patch[range(x_patch.shape[0]), order].reshape(x.shape[0], x.shape[2]//k, x.shape[3]//k, -1)
        x_patch = x_patch.permute(0, 3, 1, 2)
        x = Segment_DETR.untransform(x_patch)
        return x, order

    def forward_centroid(self, x, inference=False):
        normed_features = F.normalize(self.transform(x.detach()), dim=1)
        normed_clusters = F.normalize(self.cluster_probe, dim=1)
        inner_products = torch.einsum("bchw,nc->bnhw", normed_features, normed_clusters)

        if inference:
            return torch.argmax(inner_products, dim=1)

        cluster_probs = F.one_hot(torch.argmax(inner_products, dim=1), self.cluster_probe.shape[0]) \
            .permute(0, 3, 1, 2).to(torch.float32)

        cluster_loss = -(cluster_probs * inner_products).sum(1).mean()
        return cluster_loss, cluster_probs.argmax(1)
    
