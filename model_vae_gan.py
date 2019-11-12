# Copyright 2019 Stanislav Pidhorskyi
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import torch
from torch import nn
import random
import losses
from net import Generator, Mapping, Discriminator, Encoder, VAEMappingFromLatent, VAEMappingToLatent
import numpy as np
from gradient_reversal import grad_reverse
import torch.nn.functional as F


class DLatent(nn.Module):
    def __init__(self, dlatent_size, layer_count):
        super(DLatent, self).__init__()
        buffer = torch.zeros(layer_count, dlatent_size, dtype=torch.float32)
        self.register_buffer('buff', buffer)


class Model(nn.Module):
    def __init__(self, startf=32, maxf=256, layer_count=3, latent_size=128, mapping_layers=5, dlatent_avg_beta=None,
                 truncation_psi=None, truncation_cutoff=None, style_mixing_prob=None, channels=3):
        super(Model, self).__init__()

        self.layer_count = layer_count

        self.mapping_tl = VAEMappingToLatent(
            latent_size=latent_size,
            dlatent_size=latent_size,
            mapping_fmaps=latent_size,
            mapping_layers=mapping_layers)

        self.mapping_fl = VAEMappingFromLatent(
            num_layers=2 * layer_count,
            latent_size=latent_size,
            dlatent_size=latent_size,
            mapping_fmaps=latent_size,
            mapping_layers=mapping_layers)

        self.decoder = Generator(
            startf=startf,
            layer_count=layer_count,
            maxf=maxf,
            latent_size=latent_size,
            channels=channels)

        self.encoder = Encoder(
            startf=startf,
            layer_count=layer_count,
            maxf=maxf,
            latent_size=latent_size,
            channels=channels)

        self.dlatent_avg = DLatent(latent_size, self.mapping_fl.num_layers)
        self.latent_size = latent_size
        self.dlatent_avg_beta = dlatent_avg_beta
        self.truncation_psi = truncation_psi
        self.style_mixing_prob = style_mixing_prob
        self.truncation_cutoff = truncation_cutoff

    def generate(self, lod, blend_factor, z=None, count=32, mixing=True):
        if z is None:
            z = torch.randn(count, self.latent_size)
        styles = self.mapping_fl(z)

        if self.dlatent_avg_beta is not None:
            with torch.no_grad():
                batch_avg = styles.mean(dim=0)
                self.dlatent_avg.buff.data.lerp_(batch_avg.data, 1.0 - self.dlatent_avg_beta)

        if mixing and self.style_mixing_prob is not None:
            if random.random() < self.style_mixing_prob:
                z2 = torch.randn(count, self.latent_size)
                styles2 = self.mapping_fl(z2)

                layer_idx = torch.arange(self.mapping_fl.num_layers)[np.newaxis, :, np.newaxis]
                cur_layers = (lod + 1) * 2
                mixing_cutoff = random.randint(1, cur_layers)
                styles = torch.where(layer_idx < mixing_cutoff, styles, styles2)

        if self.truncation_psi is not None:
            layer_idx = torch.arange(self.mapping_fl.num_layers)[np.newaxis, :, np.newaxis]
            ones = torch.ones(layer_idx.shape, dtype=torch.float32)
            coefs = torch.where(layer_idx < self.truncation_cutoff, self.truncation_psi * ones, ones)
            styles = torch.lerp(self.dlatent_avg.buff.data, styles, coefs)

        rec = self.decoder.forward(styles, lod, blend_factor)
        return rec

    def reparameterize(self, mu, logvar):
        if self.training:
            std = torch.exp(0.5 * logvar)
            eps = torch.randn_like(std)
            return eps.mul(std).add_(mu)
        else:
            return mu

    def encode(self, x, lod, blend_factor):
        Z = self.encoder(x, lod, blend_factor)
        Z = self.mapping_tl(Z)

        mu, logvar = Z[:, 0], Z[:, 1]
        return mu, logvar

    def forward(self, x, lod, blend_factor, d_train):
        if d_train:
            Z = self.encode(x, lod, blend_factor)

            self.Xr = self.generate(lod, blend_factor, self.reparameterize(*Z), mixing=False)

            self.Xp = self.generate(lod, blend_factor, count=x.shape[0])

            self.Lae = losses.loss_rec(self.Xr, x, lod)

            Zr = self.encode(self.Xr.detach(), lod, blend_factor)
            Zpp = self.encode(self.Xp.detach(), lod, blend_factor)

            m = 1.0
            alpha = 0.15

            lf = (F.relu(m - losses.kl(*Zr)) + F.relu(m - losses.kl(*Zpp)))
            Ladv = losses.kl(*Z) + alpha * (F.relu(m - losses.kl(*Zr)) + F.relu(m - losses.kl(*Zpp)))
            return Ladv, self.Lae, lf
        else:
            Zr = self.encode(self.Xr, lod, blend_factor)
            Zpp = self.encode(self.Xp, lod, blend_factor)
            self.Xr = None
            self.Xp = None

            alpha = 0.15
            Ladv = alpha * (losses.kl(*Zr) + losses.kl(*Zpp))

            Lae = self.Lae
            self.Lae = None
            return Ladv, Lae


        # LklZ = losses.kl(*Z)
        #
        # loss1 = LklZ * 0.02 + Lae
        #
        # Zr = self.encoder(grad_reverse(Xr), lod, blend_factor)
        #
        # Ladv = -losses.kl(*Zr) * alpha
        #
        # loss2 = Ladv * 0.02
        #
        # autoencoder_optimizer.zero_grad()
        # (loss1 + loss2).backward()
        # autoencoder_optimizer.step()


        # if d_train:
        #     with torch.no_grad():
        #         rec = self.generate(lod, blend_factor, count=x.shape[0])
        #     self.discriminator.requires_grad_(True)
        #     d_result_real = self.discriminator(x, lod, blend_factor).squeeze()
        #     d_result_fake = self.discriminator(rec.detach(), lod, blend_factor).squeeze()
        #
        #     loss_d = losses.discriminator_logistic_simple_gp(d_result_fake, d_result_real, x)
        #     return loss_d
        # else:
        #     rec = self.generate(lod, blend_factor, count=x.shape[0])
        #     self.discriminator.requires_grad_(False)
        #     d_result_fake = self.discriminator(rec, lod, blend_factor).squeeze()
        #     loss_g = losses.generator_logistic_non_saturating(d_result_fake)
        #     return loss_g

    def lerp(self, other, betta):
        if hasattr(other, 'module'):
            other = other.module
        with torch.no_grad():
            params = list(self.mapping_tl.parameters()) + list(self.mapping_fl.parameters()) + list(self.decoder.parameters()) + list(self.encoder.parameters()) + list(self.dlatent_avg.parameters())
            other_param = list(other.mapping_tl.parameters()) + list(other.mapping_fl.parameters()) + list(other.decoder.parameters()) + list(other.encoder.parameters()) + list(other.dlatent_avg.parameters())
            for p, p_other in zip(params, other_param):
                p.data.lerp_(p_other.data, 1.0 - betta)