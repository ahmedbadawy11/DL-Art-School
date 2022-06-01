import torch
import torch.nn as nn
import torch.nn.functional as F

from models.audio.music.music_quantizer import MusicQuantizer
from models.diffusion.nn import timestep_embedding, normalization, zero_module, conv_nd, linear
from models.diffusion.unet_diffusion import TimestepBlock
from models.lucidrains.x_transformers import Encoder, Attention, FeedForward, RMSScaleShiftNorm, RotaryEmbedding
from trainer.networks import register_model
from utils.util import checkpoint, print_network


def is_latent(t):
    return t.dtype == torch.float

def is_sequence(t):
    return t.dtype == torch.long


class MultiGroupEmbedding(nn.Module):
    def __init__(self, tokens, groups, dim):
        super().__init__()
        self.m = nn.ModuleList([nn.Embedding(tokens, dim // groups) for _ in range(groups)])

    def forward(self, x):
        h = [embedding(x[:, :, i]) for i, embedding in enumerate(self.m)]
        return torch.cat(h, dim=-1)


class TimestepRotaryEmbedSequential(nn.Sequential, TimestepBlock):
    def forward(self, x, emb, rotary_emb):
        for layer in self:
            if isinstance(layer, TimestepBlock):
                x = layer(x, emb, rotary_emb)
            else:
                x = layer(x, rotary_emb)
        return x


class DietAttentionBlock(TimestepBlock):
    def __init__(self, in_dim, dim, heads, dropout):
        super().__init__()
        self.rms_scale_norm = RMSScaleShiftNorm(in_dim)
        self.proj = nn.Linear(in_dim, dim)
        self.attn = Attention(dim, heads=heads, causal=False, dropout=dropout)
        self.ff = FeedForward(dim, in_dim, mult=1, dropout=dropout, zero_init_output=True)

    def forward(self, x, timestep_emb, rotary_emb):
        h = self.rms_scale_norm(x, norm_scale_shift_inp=timestep_emb)
        h = self.proj(h)
        h, _, _, _ = checkpoint(self.attn, h, None, None, None, None, None, rotary_emb)
        h = checkpoint(self.ff, h)
        return h + x


class TransformerDiffusion(nn.Module):
    """
    A diffusion model composed entirely of stacks of transformer layers. Why would you do it any other way?
    """
    def __init__(
            self,
            prenet_channels=256,
            model_channels=512,
            block_channels=256,
            num_layers=8,
            in_channels=256,
            rotary_emb_dim=32,
            input_vec_dim=512,
            out_channels=512,  # mean and variance
            dropout=0,
            use_fp16=False,
            # Parameters for regularization.
            unconditioned_percentage=.1,  # This implements a mechanism similar to what is used in classifier-free training.
    ):
        super().__init__()

        self.in_channels = in_channels
        self.model_channels = model_channels
        self.prenet_channels = prenet_channels
        self.out_channels = out_channels
        self.dropout = dropout
        self.unconditioned_percentage = unconditioned_percentage
        self.enable_fp16 = use_fp16

        self.inp_block = conv_nd(1, in_channels, prenet_channels, 3, 1, 1)

        self.time_embed = nn.Sequential(
            linear(prenet_channels, prenet_channels),
            nn.SiLU(),
            linear(prenet_channels, prenet_channels),
        )
        prenet_heads = prenet_channels//64
        self.conditioning_embedder = nn.Sequential(nn.Conv1d(in_channels, prenet_channels // 2, 3, padding=1, stride=2),
                                                   nn.Conv1d(prenet_channels//2, prenet_channels,3,padding=1,stride=2))
        self.conditioning_encoder = Encoder(
                    dim=prenet_channels,
                    depth=4,
                    heads=prenet_heads,
                    ff_dropout=dropout,
                    attn_dropout=dropout,
                    use_rmsnorm=True,
                    ff_glu=True,
                    rotary_pos_emb=True,
                    zero_init_branch_output=True,
                    ff_mult=1,
                )

        self.input_converter = nn.Linear(input_vec_dim, prenet_channels)
        self.code_converter = Encoder(
                    dim=prenet_channels,
                    depth=3,
                    heads=prenet_heads,
                    ff_dropout=dropout,
                    attn_dropout=dropout,
                    use_rmsnorm=True,
                    ff_glu=True,
                    rotary_pos_emb=True,
                    zero_init_branch_output=True,
                    ff_mult=1,
                )

        self.unconditioned_embedding = nn.Parameter(torch.randn(1,1,prenet_channels))
        self.rotary_embeddings = RotaryEmbedding(rotary_emb_dim)
        self.cond_intg = nn.Linear(prenet_channels*2, model_channels)
        self.intg = nn.Linear(prenet_channels*2, model_channels)
        self.layers = TimestepRotaryEmbedSequential(*[DietAttentionBlock(model_channels, block_channels, block_channels // 64, dropout) for _ in range(num_layers)])

        self.out = nn.Sequential(
            normalization(model_channels),
            nn.SiLU(),
            zero_module(conv_nd(1, model_channels, out_channels, 3, padding=1)),
        )

        self.debug_codes = {}

    def get_grad_norm_parameter_groups(self):
        groups = {
            'contextual_embedder': list(self.conditioning_embedder.parameters()),
            'layers': list(self.layers.parameters()) + list(self.inp_block.parameters()),
            'code_converters': list(self.input_converter.parameters()) + list(self.code_converter.parameters()),
            'time_embed': list(self.time_embed.parameters()),
        }
        return groups

    def timestep_independent(self, codes, conditioning_input, expected_seq_len):
        cond_emb = self.conditioning_embedder(conditioning_input).permute(0,2,1)
        cond_emb = self.conditioning_encoder(cond_emb)[:, 0]
        code_emb = self.input_converter(codes)

        # Mask out the conditioning branch for whole batch elements, implementing something similar to classifier-free guidance.
        if self.training and self.unconditioned_percentage > 0:
            unconditioned_batches = torch.rand((code_emb.shape[0], 1, 1),
                                               device=code_emb.device) < self.unconditioned_percentage
            code_emb = torch.where(unconditioned_batches, self.unconditioned_embedding.repeat(codes.shape[0], 1, 1),
                                   code_emb)
        code_emb = self.code_converter(code_emb)

        expanded_code_emb = F.interpolate(code_emb.permute(0,2,1), size=expected_seq_len, mode='nearest').permute(0,2,1)
        return expanded_code_emb, cond_emb

    def forward(self, x, timesteps, codes=None, conditioning_input=None, precomputed_code_embeddings=None,
                precomputed_cond_embeddings=None, conditioning_free=False):
        if precomputed_code_embeddings is not None:
            assert codes is None and conditioning_input is None, "Do not provide precomputed embeddings and the other parameters. It is unclear what you want me to do here."

        unused_params = []
        if conditioning_free:
            code_emb = self.unconditioned_embedding.repeat(x.shape[0], x.shape[-1], 1)
            cond_emb = self.conditioning_embedder(conditioning_input).permute(0,2,1)
            cond_emb = self.conditioning_encoder(cond_emb)[:, 0]
            unused_params.extend(list(self.code_converter.parameters()))
        else:
            if precomputed_code_embeddings is not None:
                code_emb = precomputed_code_embeddings
                cond_emb = precomputed_cond_embeddings
            else:
                code_emb, cond_emb = self.timestep_independent(codes, conditioning_input, x.shape[-1])
            unused_params.append(self.unconditioned_embedding)

        blk_emb = torch.cat([self.time_embed(timestep_embedding(timesteps, self.prenet_channels)), cond_emb], dim=-1)
        blk_emb = self.cond_intg(blk_emb)
        x = self.inp_block(x).permute(0,2,1)

        rotary_pos_emb = self.rotary_embeddings(x.shape[1], x.device)
        x = self.intg(torch.cat([x, code_emb], dim=-1))
        for layer in self.layers:
            x = checkpoint(layer, x, blk_emb, rotary_pos_emb)

        x = x.float().permute(0,2,1)
        out = self.out(x)

        # Involve probabilistic or possibly unused parameters in loss so we don't get DDP errors.
        extraneous_addition = 0
        for p in unused_params:
            extraneous_addition = extraneous_addition + p.mean()
        out = out + extraneous_addition * 0

        return out


class TransformerDiffusionWithQuantizer(nn.Module):
    def __init__(self, freeze_quantizer_until=20000, **kwargs):
        super().__init__()

        self.internal_step = 0
        self.freeze_quantizer_until = freeze_quantizer_until
        self.diff = TransformerDiffusion(**kwargs)
        from models.audio.mel2vec import ContrastiveTrainingWrapper
        self.m2v = MusicQuantizer(inp_channels=256, inner_dim=2048, codevector_dim=1024)
        self.m2v.quantizer.temperature = self.m2v.min_gumbel_temperature
        del self.m2v.up

    def update_for_step(self, step, *args):
        self.internal_step = step
        qstep = max(0, self.internal_step - self.freeze_quantizer_until)
        self.m2v.quantizer.temperature = max(
                    self.m2v.max_gumbel_temperature * self.m2v.gumbel_temperature_decay**qstep,
                    self.m2v.min_gumbel_temperature,
                )

    def forward(self, x, timesteps, truth_mel, conditioning_input, conditioning_free=False):
        quant_grad_enabled = self.internal_step > self.freeze_quantizer_until
        with torch.set_grad_enabled(quant_grad_enabled):
            proj = self.m2v(truth_mel, return_decoder_latent=True).permute(0,2,1)

        # Make sure this does not cause issues in DDP by explicitly using the parameters for nothing.
        if not quant_grad_enabled:
            unused = 0
            for p in self.m2v.parameters():
                unused = unused + p.mean() * 0
            proj = proj + unused

        return self.diff(x, timesteps, codes=proj, conditioning_input=conditioning_input,
                         conditioning_free=conditioning_free)

    def get_debug_values(self, step, __):
        if self.m2v.total_codes > 0:
            return {'histogram_codes': self.m2v.codes[:self.m2v.total_codes]}
        else:
            return {}


@register_model
def register_transformer_diffusion7(opt_net, opt):
    return TransformerDiffusion(**opt_net['kwargs'])


@register_model
def register_transformer_diffusion7_with_quantizer(opt_net, opt):
    return TransformerDiffusionWithQuantizer(**opt_net['kwargs'])


"""
# For TFD5
if __name__ == '__main__':
    clip = torch.randn(2, 256, 400)
    aligned_sequence = torch.randn(2,100,512)
    cond = torch.randn(2, 256, 400)
    ts = torch.LongTensor([600, 600])
    model = TransformerDiffusion(model_channels=3072, block_channels=1536, prenet_channels=1536)
    torch.save(model, 'sample.pth')
    print_network(model)
    o = model(clip, ts, aligned_sequence, cond)
"""

if __name__ == '__main__':
    clip = torch.randn(2, 256, 400)
    cond = torch.randn(2, 256, 400)
    ts = torch.LongTensor([600, 600])
    model = TransformerDiffusionWithQuantizer(model_channels=2048, block_channels=1024, prenet_channels=1024, input_vec_dim=2048, num_layers=16)

    #quant_weights = torch.load('X:\\dlas\\experiments\\train_music_quant\\models\\1000_generator.pth')
    #diff_weights = torch.load('X:\\dlas\\experiments\\train_music_diffusion_tfd5\\models\\48000_generator_ema.pth')
    #model.m2v.load_state_dict(quant_weights, strict=False)
    #model.diff.load_state_dict(diff_weights)

    #torch.save(model.state_dict(), 'sample.pth')
    print_network(model)
    o = model(clip, ts, clip, cond)
