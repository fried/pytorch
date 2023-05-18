from itertools import product, combinations, combinations_with_replacement, permutations
import torch
import torch.utils.benchmark as benchmark
from torch import nn
from torch.ao.pruning import WeightNormPruner
from torch.ao.nn.sparse.cusparselt_linear import cuSPARSELtLinear, cuSPARSELtLinearInt8
from tqdm import tqdm
import pandas as pd
import argparse
import random


DEVICE = "cuda"
torch.set_printoptions(
    precision=2,
    threshold=None,
    edgeitems=16,
    linewidth=480,
    profile=None,
    sci_mode=False,
)


# helper model definition for pruner
class Model(nn.Module):
    def __init__(self, m, k, dtype=None):
        super().__init__()
        # transposed so reversed
        self.linear = nn.Linear(k, m)

    def forward(self, x):
        return self.linear(x)


def gen_two_four_sparse_mask(m, k, dtype):
    def random_mask_choice(i=None):
        choices = [
            [1, 1, 0, 0],
            [1, 0, 1, 0],
            [1, 0, 0, 1],
            [0, 1, 1, 0],
            [0, 1, 0, 1],
            [1, 0, 1, 0],
        ]
        return choices[random.randint(0, len(choices) - 1) if i is None else i]

    mask_entries = [random_mask_choice() for i in range(m * k // 4)]
    return (
        torch.tensor(mask_entries, dtype=dtype, device=DEVICE).view(m, k).contiguous()
    )


class SemiSparseTensor(torch.Tensor):
    @staticmethod
    def __new__(
        cls,
        original_tensor,
        original_shape=None,
        compressed_tensor=None,
        cslt=None,
        transposed=False,
    ):
        kwargs = {}
        kwargs["device"] = (
            original_tensor.device
            if original_tensor is not None
            else compressed_tensor.device
        )
        kwargs["dtype"] = (
            original_tensor.dtype
            if original_tensor is not None
            else compressed_tensor.dtype
        )
        kwargs["layout"] = (
            original_tensor.layout
            if original_tensor is not None
            else compressed_tensor.layout
        )
        kwargs["requires_grad"] = (
            original_tensor.requires_grad if original_tensor is not None else False
        )

        shape = original_shape if original_shape is not None else original_tensor.shape

        return torch.Tensor._make_wrapper_subclass(cls, shape, **kwargs)

    def __init__(
        self,
        original_tensor,
        original_shape=None,
        compressed_tensor=None,
        cslt=None,
        transposed=False,
    ):
        self.original_tensor = original_tensor
        self.original_shape = (
            original_tensor.shape if original_shape is None else original_shape
        )
        self.compressed_tensor = compressed_tensor
        self.cslt = cslt
        self.transposed = transposed

        if original_tensor is not None:
            num_bytes = original_tensor.nelement() * original_tensor.element_size()
            if original_tensor.dtype == torch.float16:
                compression_factor = 9
            elif original_tensor.dtype == torch.int8:
                compression_factor = 10
            compressed_size_bytes = num_bytes * compression_factor // 16
            compressed_size = compressed_size_bytes // original_tensor.element_size()

            self.compressed_tensor = torch.empty(
                (compressed_size,),
                dtype=original_tensor.dtype,
                device=original_tensor.device,
            )

            self.cslt = torch.cusparselt.CusparseLt(self.compressed_tensor)
            self.cslt.set_compressed(original_tensor, self.transposed)
            self.original_tensor = None

    def __repr__(self):
        return f"SemiSparseTensor(shape={self.shape})"

    __torch_function__ = torch._C._disabled_torch_function_impl

    @classmethod
    def __torch_dispatch__(cls, func, types, args, kwargs):
        if func is torch.ops.aten.detach.default:
            # since we create a new compressed tensor, the tensor will already be detached. 
            return SemiSparseTensor(
                args[0].original_tensor,
                compressed_tensor=args[0].compressed_tensor,
                cslt=args[0].cslt,
                original_shape=args[0].original_shape,
                transposed=args[0].transposed,
            )

        if func is torch.ops.aten.t.default:
            # Because we cannot go from the compressed representation back to the dense representation currently, we just keep track of how many times we have been transposed. If it's an odd number of times, we'll throw an error since we can't handle this situation
            return SemiSparseTensor(
                args[0].original_tensor,
                compressed_tensor=args[0].compressed_tensor,
                cslt=args[0].cslt,
                original_shape=args[0].original_shape,
                transposed=not args[0].transposed,
            )

        if (
            func is torch.ops.aten.addmm.default
            and args[0].is_floating_point()
            and args[0].is_cuda
        ):
            bias, a, b = args
            if isinstance(a, SemiSparseTensor) and not a.transposed:
                # CURRENTLY BIAS is broadcasted the wrong way in cuSPARSELT
                return a.cslt.cusparselt_addmm(b, bias)
            # b must be transposed so we can undo it
            elif isinstance(b, SemiSparseTensor) and b.transposed:
                return b.t().cslt.cusparselt_addmm(a.T, bias)

        if func is torch.ops.aten.mm.default:
            a, b = args
            if isinstance(a, SemiSparseTensor) and not a.transposed:
                return a.cslt.cusparselt_mm(b)
            elif isinstance(b, SemiSparseTensor) and b.transposed:
                return b.t().cslt.cusparselt_mm(a.T).T

        raise NotImplementedError("Not implemented")


def test_linear(m, k, n, dtype):
    A = torch.rand(m, k).half().cuda() * gen_two_four_sparse_mask(m, k, torch.float16)
    B = torch.zeros(n, k).half().cuda()

    sA = SemiSparseTensor(A)

    model = Model(m, k).half().cuda().eval()
    model.weight = A
    # print(model.linear.weight.shape)
    # print(model.linear.bias.shape)
    # get latency
    dense_measurement = benchmark.Timer(
        stmt="model(input_tensor)",
        globals={"input_tensor": B, "model": model},
    ).blocked_autorange()

    temp = model(B)

    model.linear.weight = nn.Parameter(SemiSparseTensor(model.linear.weight))
    res = model(B)

    # print(temp.is_contiguous())
    # print(res.is_contiguous())
    # print(temp)
    # print(res)

    sparse_measurement = benchmark.Timer(
        stmt="model(input_tensor)",
        globals={"input_tensor": B, "model": model},
    ).blocked_autorange()

    correct = torch.allclose(temp, res, rtol=1e-3, atol=1e-3)

    return {
        "m": m,
        "k": k,
        "n": n,
        # "eval_batch_size": batch_size,
        # "init_batch_size": init_batch_size,
        "dtype": str(dtype),
        "sparse_latency (ms)": sparse_measurement.median * 1001,
        "dense_latency (ms)": dense_measurement.median * 1000,
        "speedup (d/s)": dense_measurement.median / sparse_measurement.median,
        "correct": correct,
    }


def test_tensor(m, k, n, dtype):
    A = gen_two_four_sparse_mask(m, k, torch.float16)
    B = torch.zeros(k, n).half().cuda()
    bias = torch.rand(n).half().cuda()

    sA = SemiSparseTensor(A)

    # torch.mm calculation
    sparse_output_addmm = torch.addmm(bias, sA, B)
    dense_output_addmm = torch.addmm(bias, A, B)
    correct_addmm = torch.allclose(
        sparse_output_addmm, dense_output_addmm, rtol=1e-3, atol=1e-3
    )
    # print(dense_output_addmm)
    # print(sparse_output_addmm)

    dense_output = torch.mm(A, B)
    dense_measurement = benchmark.Timer(
        stmt="torch.addmm(bias, A, B)",
        globals={"A": A, "B": B, "bias": bias},
    ).blocked_autorange()

    sparse_output = torch.mm(sA, B)

    correct = torch.allclose(dense_output, sparse_output, rtol=1e-3, atol=1e-3)

    print(correct, correct_addmm)

    sparse_measurement = benchmark.Timer(
        stmt="torch.addmm(bias, sA, B)",
        globals={"sA": sA, "B": B, "bias": bias},
    ).blocked_autorange()

    return {
        "m": m,
        "k": k,
        "n": n,
        # "eval_batch_size": batch_size,
        # "init_batch_size": init_batch_size,
        "dtype": str(dtype),
        "sparse_latency (ms)": sparse_measurement.median * 1001,
        "dense_latency (ms)": dense_measurement.median * 1000,
        "speedup (d/s)": dense_measurement.median / sparse_measurement.median,
        "correct": correct,
    }


def compare_linear(m, k, n, batch_size, init_batch_size, dtype, assert_correct=False):
    # function to compare dense vs cusparselt linear for given m, k, n, batch_size
    temp = cuSPARSELtLinear if dtype is torch.float16 else cuSPARSELtLinearInt8

    # print(m, k, n, batch_size, init_batch_size, dtype, temp)
    # create dense fp16 model
    model = Model(m, k).half().cuda().eval()

    # need to set model weight since int8 and also clear out bias
    # this is because you can't have a int8 linear layer currently, dispatch wont work on int8 matmul
    if dtype is torch.int8:
        model.linear.bias.data.zero_()
        model.linear.weight.data = gen_two_four_sparse_mask(m, k, torch.float16)

    # create input tensor
    input_tensor = torch.randint(
        2,
        (init_batch_size, n, k),
        device=DEVICE,
        dtype=dtype,
    )

    # get sparse model
    pruner = WeightNormPruner(
        sparsity_level=1.0, sparse_block_shape=(1, 4), zeros_per_block=2
    )

    pruner.prepare(model, [{"tensor_fqn": "linear.weight"}])
    pruner.step()
    # sparse_model = pruner.convert(model, mapping={nn.Linear: temp})
    pruner.squash_mask()
    model.linear.weight = nn.Parameter(SemiSparseTensor(model.linear.weight))

    # print(input_tensor)

    sparse_output = sparse_model(input_tensor)
    dense_output = model(input_tensor.half()).to(dtype)
    # print(sparse_output)
    # print(dense_output)

    # print(sparse_model.linear.weight)
    # print(model.linear.weight)

    correct = torch.allclose(dense_output, sparse_output, rtol=1e-3, atol=1e-3)

    input_tensor = torch.randint(
        2,
        (batch_size, n, k),
        device=DEVICE,
        dtype=dtype,
    )
    # get latency
    sparse_measurement = benchmark.Timer(
        stmt="sparse_model(input_tensor)",
        globals={"input_tensor": input_tensor, "sparse_model": sparse_model},
    ).blocked_autorange()
    dense_measurement = benchmark.Timer(
        stmt="model(input_tensor)",
        globals={"input_tensor": input_tensor.half(), "model": model},
    ).blocked_autorange()

    return {
        "m": m,
        "k": k,
        "n": n,
        "eval_batch_size": batch_size,
        "init_batch_size": init_batch_size,
        "dtype": str(dtype),
        "sparse_latency (ms)": sparse_measurement.median * 1000,
        "dense_latency (ms)": dense_measurement.median * 1000,
        "speedup (d/s)": dense_measurement.median / sparse_measurement.median,
        "correct": correct,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cuSPARSELt Benchmarks")
    parser.add_argument(
        "--mode",
        type=str,
        choices=[
            "nvidia-bert",
            "nvidia-fixed-k",
            "nvidia-fixed-mn",
            "llama-shapes",
            "int8",
            "test",
        ],
    )
    args = parser.parse_args()

    print(f"Started benchmark: {args.mode}")

    if args.mode == "nvidia-bert":
        bert_shapes = [
            (3072, 1024, 16384),
            (4096, 1024, 16384),
            (1024, 1024, 16384),
            (1024, 4096, 16384),
        ]
        results = (
            test_linear(m, k, n, torch.float16) for (m, k, n) in tqdm(bert_shapes)
        )

    elif args.mode == "nvidia-fixed-k":
        mn_vals = [
            3072,
            4096,
            5120,
            6144,
            7168,
            8192,
            9216,
            10240,
            11264,
            12288,
            13312,
            14336,
            15360,
            16384,
            17408,
            18432,
            19456,
            20480,
        ]
        results = (test_linear(mn, 10240, mn, torch.float16) for mn in tqdm(mn_vals))

    elif args.mode == "nvidia-fixed-mn":
        k_vals = [
            2560,
            3840,
            5120,
            6400,
            7680,
            8960,
            10240,
            11520,
            12800,
            14080,
            15360,
            16640,
            17920,
            19200,
            20480,
        ]
        results = (test_linear(10240, k, 10240, torch.float16) for k in tqdm(k_vals))

    elif args.mode == "llama-shapes":
        MP = 8
        BS = 512
        print(f"Working on MP: {MP}, BS: {BS}")
        shapes = [
            (8192 // MP, 8192, BS),
            (8192, 8192 // MP, BS),
            (22016 // MP, 8192, BS),
            (8192, 22016 // MP, BS),
        ]
        dtypes = [torch.int8, torch.float16]
        batch_sizes = [1, 16, 64, 256]
        results = (
            compare_linear(m, k, n, batch_size, batch_size, dtype)
            for dtype, batch_size, (m, k, n) in tqdm(
                product(dtypes, batch_sizes, shapes),
                total=len(dtypes) * len(batch_sizes) * len(shapes),
            )
        )

    elif args.mode == "int8":
        shapes = [(128, 128, 128)]
        dtypes = [torch.int8, torch.float16]
        batch_sizes = [1, 16, 64, 256]
        results = (
            compare_linear(m, k, n, batch_size, batch_size, dtype)
            for dtype, batch_size, (m, k, n) in tqdm(
                product(dtypes, batch_sizes, shapes),
                total=len(dtypes) * len(batch_sizes) * len(shapes),
            )
        )

    elif args.mode == "test":
        results = [test_tensor(64, 128, 64, torch.float16)]
    save_file = f"{args.mode}.csv"

    df = pd.DataFrame.from_records(results)
    df.to_csv(save_file)
    print(f"Finished benchmark: {args.mode} saved results to {save_file}")
    print(df)
