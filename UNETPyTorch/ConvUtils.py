import torch
import torch.nn as nn


def conv_output_size(input_size, kernel_size, stride=1, padding=0, dilation=1):
    return (input_size + 2 * padding - dilation * (kernel_size - 1) - 1) // stride + 1


def conv_transpose_output_size(input_size, kernel_size, stride=1, padding=0, dilation=1, output_padding=0):
    return (input_size - 1) * stride - 2 * padding + dilation * (kernel_size - 1) + output_padding + 1


# Simplified version of ConvNd using this as a guide: https://github.com/pvjosue/pytorch_convNd/blob/master/convNd.py
class ConvNd(nn.Module):
    def __init__(self, dimensions, in_channels, out_channels, kernel_size, padding=0):
        super().__init__()

        # Assert dimensions is higher than three
        assert dimensions > 3, "This block is only for cases where the dimensions are higher than 3."

        # Assert kernel size and padding are ints
        assert isinstance(kernel_size, int), "Kernel size must be an int."
        assert isinstance(padding, int), "Padding must be an int."

        # Dimension count
        self.dimensions = dimensions

        # Things to feed into the Conv block
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size  # Assumes kernel is square
        self.padding = padding  # Assumes padding is same

        # Lower dimension representation through recursion until hitting 4D, then just 3D + 1D.
        if self.dimensions > 4:
            self.lower_name = f'lower_{self.dimensions - 1}'
            setattr(self, self.lower_name, ConvNd(
                self.dimensions - 1, self.in_channels, self.out_channels, self.kernel_size, self.padding
            ))
        else:
            self.lower_name = 'lower'
            setattr(self, self.lower_name, nn.Conv3d(
                in_channels=self.in_channels, out_channels=self.out_channels,
                kernel_size=self.kernel_size, padding=self.padding
            ))

        # Capture the last dimension left out by the lower representation
        self.last_dim = nn.Conv1d(
            in_channels=self.out_channels, out_channels=self.out_channels,
            kernel_size=self.kernel_size, padding=self.padding
        )

    def forward(self, nd_tensor):
        # Get shape of tensor and shape of tensor's lower dimensions
        shape = list(nd_tensor.shape)
        lower_dim_shape = shape[-self.dimensions + 1:]

        # Adjust the view to merge the batch and highest dimension, then the channels, then the lower dimensions
        lower_tensor = nd_tensor.view(shape[0] * shape[2], shape[1], *lower_dim_shape)

        # Work the lower dimension
        lower_tensor = getattr(self, self.lower_name)(lower_tensor)

        # Recalculate the output of lower dimension shapes to account for convolution
        lower_dim_shape = [
            conv_output_size(dim, self.kernel_size, padding=self.padding) for dim in lower_dim_shape
        ]

        # Change everything back
        lower_conv_tensor = lower_tensor.view(shape[0], shape[2], -1, *lower_dim_shape)

        # Create a list from 0 to n dimensions
        # The list should go batch, highest dimension, channels, lower dimensions
        order = [i for i in range(self.dimensions + 2)]

        # Move the channels and highest dimension behind the lower dimensions, then reduce the batch to 1 dimension
        # First though, count how many scalars should fit into the lower-view batch dimension using the actual lower
        #   dimensional shape
        one_dim_count = shape[0]
        for dim_shape in lower_dim_shape:
            one_dim_count *= dim_shape

        one_dim_tensor = lower_conv_tensor.permute(0, *order[-self.dimensions + 1:], 2, 1)
        one_dim_tensor = one_dim_tensor.reshape(one_dim_count, -1, shape[2])

        # Work the last dimension to obtain connections between N-1D dimensions
        one_dim_conv_tensor = self.last_dim(one_dim_tensor)

        # The shape is currently batch, lower dimensions, channel, then highest dimension
        # Reshape everything back to normal
        high_dim_size = conv_output_size(shape[2], self.kernel_size, padding=self.padding)
        final_tensor = one_dim_conv_tensor.view(shape[0], *lower_dim_shape, self.kernel_size, high_dim_size)
        final_tensor = final_tensor.permute(0, order[-2], order[-1], *order[1:-2])
        return final_tensor


class ConvTransposeNd(nn.Module):
    def __init__(self, dimensions, in_channels, out_channels, kernel_size, padding=0):
        super().__init__()

        # Assert dimensions is higher than three
        assert dimensions > 3, "This block is only for cases where the dimensions are higher than 3."

        # Assert kernel size and padding are ints
        assert isinstance(kernel_size, int), "Kernel size must be an int."
        assert isinstance(padding, int), "Padding must be an int."

        # Dimension count
        self.dimensions = dimensions

        # Things to feed into the Conv block
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size  # Assumes kernel is square
        self.padding = padding  # Assumes padding is same

        # Lower dimension representation through recursion until hitting 4D, then just 3D + 1D.
        if self.dimensions > 4:
            self.lower_name = f'lower_{self.dimensions - 1}'
            setattr(self, self.lower_name, ConvTransposeNd(
                self.dimensions - 1, self.in_channels, self.out_channels, self.kernel_size, self.padding
            ))
        else:
            self.lower_name = 'lower'
            setattr(self, self.lower_name, nn.ConvTranspose3d(
                in_channels=self.in_channels, out_channels=self.out_channels,
                kernel_size=self.kernel_size, padding=self.padding
            ))

        # Capture the last dimension left out by the lower representation
        self.last_dim = nn.ConvTranspose1d(
            in_channels=self.out_channels, out_channels=self.out_channels,
            kernel_size=self.kernel_size, padding=self.padding
        )

    def forward(self, nd_tensor):
        # Get shape of tensor and shape of tensor's lower dimensions
        shape = list(nd_tensor.shape)
        lower_dim_shape = shape[-self.dimensions + 1:]

        # Adjust the view to merge the batch and highest dimension, then the channels, then the lower dimensions
        lower_tensor = nd_tensor.view(shape[0] * shape[2], shape[1], *lower_dim_shape)

        # Work the lower dimension
        lower_tensor = getattr(self, self.lower_name)(lower_tensor)

        # Recalculate the output of lower dimension shapes to account for convolution
        lower_dim_shape = [
            conv_transpose_output_size(dim, self.kernel_size, padding=self.padding) for dim in lower_dim_shape
        ]

        # Change everything back
        lower_conv_tensor = lower_tensor.view(shape[0], shape[2], -1, *lower_dim_shape)

        # Create a list from 0 to n dimensions
        # The list should go batch, highest dimension, channels, lower dimensions
        order = [i for i in range(self.dimensions + 2)]

        # Move the channels and highest dimension behind the lower dimensions, then reduce the batch to 1 dimension
        # First though, count how many scalars should fit into the lower-view batch dimension using the actual lower
        #   dimensional shape
        one_dim_count = shape[0]
        for dim_shape in lower_dim_shape:
            one_dim_count *= dim_shape

        one_dim_tensor = lower_conv_tensor.permute(0, *order[-self.dimensions + 1:], 2, 1)
        one_dim_tensor = one_dim_tensor.reshape(one_dim_count, -1, shape[2])

        # Work the last dimension to obtain connections between N-1D dimensions
        one_dim_conv_tensor = self.last_dim(one_dim_tensor)

        # The shape is currently batch, lower dimensions, channel, then highest dimension
        # Reshape everything back to normal
        high_dim_size = conv_transpose_output_size(shape[2], self.kernel_size, padding=self.padding)
        final_tensor = one_dim_conv_tensor.view(shape[0], *lower_dim_shape, self.kernel_size, high_dim_size)
        final_tensor = final_tensor.permute(0, order[-2], order[-1], *order[1:-2])
        return final_tensor


# TODO: Viable by restructuring channel content to 1D, conducting convolution, then reshaping back
class BatchNormNd(nn.Module):
    ...


# TODO: Viable using same derivative property as convolution
class MaxPoolNd(nn.Module):
    ...


# TODO: Viable using same derivative property as convolution
class AvgPoolNd(nn.Module):
    ...


if __name__ == '__main__':
    # Make 4D Tensor
    tensor = torch.randn(2, 1, 3, 4, 5, 6)

    # Convolve
    conv = ConvNd(4, 1, 3, 3)
    conv_tensor = conv(tensor)

    print(tensor.shape)
    print(conv_tensor.shape)
