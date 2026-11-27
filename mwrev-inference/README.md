# Inkling inference on Modal

The private `mwrev-inkling` web function serves the pinned Inkling 2 Mini
checkpoint on one L40S. Wrev uses the streaming Responses API and the Inkling
reasoning and tool parsers. The checkpoint revision and model weights are
unchanged. The vLLM image contains the upstream
[SM89/Ada backend](https://github.com/vllm-project/vllm/pull/69165). The
L40S uses 16-bit activations and KV cache with the mixed NVFP4/MXFP8 checkpoint,
rather than native Blackwell FP4 math.

The context limit is 131,072 tokens **including output**. The app can accept
four concurrent HTTP inputs; requests share a finite KV cache, so four
maximum-length requests need not fit simultaneously. It scales to zero with
a 60-second idle window. Modal proxy authentication stays required, and the
existing environment-scoped weights volume is reused. The guest never receives
the proxy credentials.

Local release checks do not need a GPU:

```sh
python -m unittest discover -s mwrev-inference -v
```
