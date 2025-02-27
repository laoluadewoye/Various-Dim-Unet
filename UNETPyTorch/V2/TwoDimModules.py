import sys
import os
import copy
from torchinfo import summary
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from EmbedAttnUtils import *


class ConvSetTwo(nn.Module):
    def __init__(self, channel_sequence, kernel_sequence, padding_sequence, stride=1, act_function=None,
                 use_time=False, time_embed_count=0, attention_args=None, use_res=False):
        super().__init__()

        # Assert that the channel sequence is at least 3 items long (for double convolution)
        assert len(channel_sequence) >= 3, "Sequence of channels must be at least three for a proper ConvSet."

        # Assert that the kernel sequence and padding sequence is at least 2 items long (for double convolution)
        assert len(kernel_sequence) >= 2, "Sequence of kernels must be at least two for a proper ConvSet."
        assert len(padding_sequence) >= 2, "Sequence of padding must be at least two for a proper ConvSet."

        # Assert that the kernels, padding, and channel (minus one) are the same length
        assert len(kernel_sequence) == len(padding_sequence) == (len(channel_sequence) - 1), (
            "Kernels and padding must be the same length."
        )

        # List of convolutions to conduct
        conv_list = []
        for i in range(len(channel_sequence) - 1):
            # Set the stride. Mainly needed for ResNet
            if i == 1:
                cur_stride = stride
            else:
                cur_stride = 1

            # Add the convolution module
            conv_list.append(nn.Sequential(
                nn.Conv2d(
                    channel_sequence[i], channel_sequence[i+1], kernel_size=kernel_sequence[i],
                    padding=padding_sequence[i], stride=cur_stride
                ),
                nn.BatchNorm2d(channel_sequence[i+1]),
                copy.deepcopy(act_function) if act_function is not None else nn.ReLU(inplace=True)
            ))

        # Get it recognized by PyTorch
        self.conv_list = nn.ModuleList(conv_list)

        # Optional time modification after first convolution
        self.need_time = use_time
        if self.need_time:
            self.embed_adjuster = nn.Sequential(
                nn.Linear(time_embed_count, channel_sequence[2]),
                nn.ReLU()
            )
        else:
            self.embed_adjuster = None

        # Setting attention
        if attention_args is not None:
            self.attention = Attention(**attention_args)
        else:
            self.attention = None

        # Optional residual modification
        self.need_res = use_res
        if self.need_res:
            self.res_match = nn.Sequential(
                nn.Conv2d(channel_sequence[0], channel_sequence[-1], kernel_size=1, stride=stride),
                nn.BatchNorm2d(channel_sequence[-1]),
            )
            self.res_act = nn.ReLU(inplace=True)
        else:
            self.res_match = None
            self.res_act = None

    def forward(self, batch, time_embed=None):
        # Do first convolution set
        conv_batch = self.conv_list[0](batch)

        # Create time embedding if needed
        if self.need_time:
            # Assert that the time_embed is not none
            assert time_embed is not None, (
                "Time embedding is not provided for convolution steps.\n"
                f"\tConvSet Layer info: {self}.\n\n"
            )

            # Retrieves the embed given a time step
            adjusted_time_embed = self.embed_adjuster(time_embed)

            # Expands the shape to (batch, out_channels, 1, 1)
            adjusted_time_embed = adjusted_time_embed[(...,) + (None,) * 2]

            # Adds time-sensitive embeddings to batch
            conv_batch = conv_batch + adjusted_time_embed

        # Do the rest of the conv sets
        for i in range(1, len(self.conv_list)):
            conv_batch = self.conv_list[i](conv_batch)

        # Apply attention if needed
        if self.attention is not None:
            conv_batch_attn = self.attention(conv_batch.reshape(conv_batch.shape[0], conv_batch.shape[1], -1))
            conv_batch = conv_batch_attn.reshape(*conv_batch.shape)

        # Add residuals if needed
        if self.need_res:
            res_batch = self.res_match(batch)
            conv_batch = self.res_act(conv_batch + res_batch)

        return conv_batch


class DownSampleTwo(nn.Module):
    def __init__(self, channel_sequence, kernel_sequence, padding_sequence, conv_act_fn=None,
                 conv_time=False, conv_time_embed_count=0, conv_attn_args=None, conv_res=False):
        super().__init__()

        # Convolution Step
        self.conv = ConvSetTwo(
            channel_sequence, kernel_sequence, padding_sequence, act_function=conv_act_fn, use_time=conv_time,
            time_embed_count=conv_time_embed_count, attention_args=conv_attn_args, use_res=conv_res
        )

        # 2x2 Max Pooling to Shrink image
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)

    def forward(self, batch, time_embed=None):
        # Channel change for skip connection
        conv_batch = self.conv(batch, time_embed)

        # Downsample for next step
        encoded_batch = self.pool(conv_batch)

        return conv_batch, encoded_batch


class UpSampleTwo(nn.Module):
    def __init__(self, channel_sequence, kernel_sequence, padding_sequence, up_drop_perc=0.3, attention_args=None,
                 conv_act_fn=None, conv_time=False, conv_time_embed_count=0, conv_attn_args=None, conv_res=False):
        super().__init__()

        # Setting attention
        if attention_args is not None:
            self.attention = Attention(**attention_args)
        else:
            self.attention = None

        # 2x2 Upscale with channel shrinkage, plus normalization, relu activation, and dropouts
        upscaler = [
            nn.ConvTranspose2d(channel_sequence[0], channel_sequence[-1], kernel_size=2, stride=2),
            nn.BatchNorm2d(channel_sequence[-1]),
            nn.ReLU(inplace=True),
        ]
        if up_drop_perc > 0:
            upscaler.append(nn.Dropout(up_drop_perc))
        self.upscaler = nn.Sequential(*upscaler)

        # Convolution Step (assumes skip connection is present to combine long and short paths)
        self.conv = ConvSetTwo(
            channel_sequence, kernel_sequence, padding_sequence, act_function=conv_act_fn, use_time=conv_time,
            time_embed_count=conv_time_embed_count, attention_args=conv_attn_args, use_res=conv_res
        )

    def forward(self, cur, skip, time_embed=None):
        # Apply attention block to skip connection if needed
        if self.attention is not None:
            attn_skip = self.attention(
                cur.reshape(cur.shape[0], cur.shape[1], -1),
                skip.reshape(skip.shape[0], skip.shape[1], -1)
            )
            skip = attn_skip.reshape(*skip.shape)

        # Upscale encoding
        cur_upscaled = self.upscaler(cur)

        # Combine results then final convolution
        combined = torch.cat([cur_upscaled, skip], 1)
        return self.conv(combined, time_embed)


class UNETTwo(nn.Module):
    def __init__(self, in_channels, channel_list, in_layer=None, out_layer=None, denoise_diff=False,
                 denoise_embed_count=0, up_drop_perc=0.3, up_attn_args=None, conv_act_fn=None, conv_attn_args=None,
                 conv_res=False):
        super().__init__()

        # Set custom input layer
        self.in_layer = in_layer if in_layer is not None else nn.Identity()

        # Create Sinusoidal Time Embedding
        self.need_denoise = denoise_diff
        if self.need_denoise:
            self.time_embeds = nn.Sequential(
                DiffPosEmbeds(denoise_embed_count, theta=10000),
                nn.Linear(denoise_embed_count, denoise_embed_count),
                nn.ReLU(inplace=True)
            )

        # Create down samplers using nn.ModuleList
        down_smap = []
        for i in range(len(channel_list) - 1):
            cur_in_channels = in_channels if i == 0 else channel_list[i - 1]
            cur_seq = [cur_in_channels, channel_list[i], channel_list[i]]
            down_smap.append(
                self.create_downsampler(cur_seq, conv_attn_args, conv_act_fn, denoise_embed_count, conv_res)
            )
        self.down_samplers = nn.ModuleList(down_smap)

        # Create bottleneck transition and attention if needed
        bn_seq = [channel_list[-2], channel_list[-1], channel_list[-1]]
        self.bottle_neck = ConvSetTwo(
            bn_seq, kernel_sequence=(3, 3), padding_sequence=(1, 1), act_function=conv_act_fn,
            use_time=self.need_denoise, time_embed_count=denoise_embed_count, use_res=conv_res
        )
        if up_attn_args is not None:
            up_attn_args['enc_channels'] = channel_list[-1]
            up_attn_args['skip_channels'] = channel_list[-1]
            up_attn_args['spatial_inter_channels'] = channel_list[-1] // 2
            self.bottle_neck_attn = Attention(**up_attn_args)
        else:
            self.bottle_neck_attn = None

        # Create up samplers using nn.ModuleList
        up_samp = []
        for i in range(len(channel_list) - 1, 0, -1):
            cur_seq = [channel_list[i], channel_list[i-1], channel_list[i-1]]
            up_samp.append(self.create_upsampler(
                cur_seq, i, up_drop_perc, up_attn_args, conv_attn_args, conv_act_fn, denoise_embed_count, conv_res
            ))
        self.up_samplers = nn.ModuleList(up_samp)

        # Set custom output layer
        self.out_layer = out_layer if out_layer is not None else nn.Identity()

    def create_downsampler(self, cur_seq, conv_attn_args, conv_act_fn, denoise_embed_count, conv_res):
        cur_conv_attn = None
        if conv_attn_args is not None:
            conv_attn_args['enc_channels'] = cur_seq[-1]
            conv_attn_args['skip_channels'] = cur_seq[-1]
            conv_attn_args['spatial_inter_channels'] = cur_seq[-1] // 2
            cur_conv_attn = conv_attn_args

        return DownSampleTwo(
            cur_seq, kernel_sequence=(3, 3), padding_sequence=(1, 1), conv_act_fn=conv_act_fn,
            conv_time=self.need_denoise, conv_time_embed_count=denoise_embed_count, conv_attn_args=cur_conv_attn,
            conv_res=conv_res
        )

    def create_upsampler(self, cur_seq, cur_index, up_drop_perc, up_attn_args, conv_attn_args, conv_act_fn,
                         denoise_embed_count, conv_res):
        cur_up_attn = None
        if up_attn_args is not None and cur_index > 1:
            up_attn_args['enc_channels'] = cur_seq[0]
            up_attn_args['skip_channels'] = cur_seq[-1]
            up_attn_args['spatial_inter_channels'] = cur_seq[-1]
            cur_up_attn = up_attn_args

        cur_conv_attn = None
        if conv_attn_args is not None:
            conv_attn_args['enc_channels'] = cur_seq[-1]
            conv_attn_args['skip_channels'] = cur_seq[-1]
            conv_attn_args['spatial_inter_channels'] = cur_seq[-1]
            cur_conv_attn = conv_attn_args

        return UpSampleTwo(
            cur_seq, kernel_sequence=(3, 3), padding_sequence=(1, 1), up_drop_perc=up_drop_perc,
            attention_args=cur_up_attn, conv_act_fn=conv_act_fn, conv_time=self.need_denoise,
            conv_time_embed_count=denoise_embed_count, conv_attn_args=cur_conv_attn, conv_res=conv_res
        )

    def forward(self, batch, time_step=None):
        # Prepare encoder
        skip_connections = []
        cur_down = self.in_layer(batch)

        # Get time embedding if needed
        if self.need_denoise:
            time_embed = self.time_embeds(time_step)
        else:
            time_embed = None

        # Downsample through list
        for down_sampler in self.down_samplers:
            down_skip, down_encoded = down_sampler(cur_down, time_embed)
            skip_connections.append(down_skip)
            cur_down = down_encoded

        # Bottleneck phase
        cur_up = self.bottle_neck(cur_down, time_embed)
        if self.bottle_neck_attn is not None:
            cur_up_attn = self.bottle_neck_attn(cur_up.reshape(cur_up.shape[0], cur_up.shape[1], -1))
            cur_up = cur_up_attn.reshape(*cur_up.shape)

        # Upsample through list
        for up_sampler in self.up_samplers:
            cur_up = up_sampler(cur_up, skip_connections.pop(), time_embed)

        # Apply custom output layer and return
        return self.out_layer(cur_up)


class ResNetTwo(nn.Module):
    ...


def make_res_net_layer(channel_sequence: list, kernel_sequence, padding_sequence, set_count, stride):
    set_list = [ConvSetTwo(channel_sequence, kernel_sequence, padding_sequence, stride=stride, use_res=True)]
    channel_sequence[0] = channel_sequence[-1]

    for i in range(set_count - 1):
        set_list.append(ConvSetTwo(channel_sequence, kernel_sequence, padding_sequence, use_res=True))

    return nn.Sequential(*set_list)


def res_net_fifty():
    # Create test conv sets
    test_res_net_module_one = make_res_net_layer(
        [3, 64, 64, 256], (1, 3, 1), (0, 1, 0), 3, 1
    )
    test_res_net_module_two = make_res_net_layer(
        [256, 128, 128, 512], (1, 3, 1), (0, 1, 0), 4, 2
    )
    test_res_net_module_three = make_res_net_layer(
        [512, 256, 256, 1024], (1, 3, 1), (0, 1, 0), 6, 2
    )
    test_res_net_module_four = make_res_net_layer(
        [1024, 512, 512, 2048], (1, 3, 1), (0, 1, 0), 3, 2
    )

    res_net_fifty = nn.Sequential(
        test_res_net_module_one, test_res_net_module_two, test_res_net_module_three, test_res_net_module_four
    )
    summary(res_net_fifty, (4, 3, 64, 64), depth=5)


if __name__ == "__main__":
    # Set the aspect size and channels
    data_size = 256
    channels = 3
    batch_size = 1

    # Create a test UNET that uses CBAM Residual Convolution Blocks and Upscaling Transformer Blocks
    cbam_args = {
        'attn_order': [AttentionOptions.CHANNEL, AttentionOptions.SPATIAL], 'use_pos': True,
        'pos_max_len': data_size*data_size
    }
    transformer_args = {
        'attn_order': [AttentionOptions.QKV], 'qkv_heads': 8, 'use_pos': True, 'pos_max_len': data_size*data_size
    }
    model = UNETTwo(
        in_channels=channels, channel_list=[64, 128, 256, 512, 1024], out_layer=nn.Sigmoid(), denoise_diff=True,
        denoise_embed_count=32, up_drop_perc=0.5, up_attn_args=transformer_args,
        conv_act_fn=nn.LeakyReLU(0.2, inplace=True), conv_attn_args=cbam_args, conv_res=True
    )

    # View model summary
    data = torch.randn(batch_size, channels, data_size, data_size)
    time_steps = torch.randint(0, 300, (batch_size,))
    summary(model, input_data=(data, time_steps,), depth=10)
