import os, sys
import mindspore as ms
__dir__ = os.path.dirname(os.path.abspath(__file__))
mindone_lib_path = os.path.abspath(os.path.join(__dir__, "../../"))
sys.path.insert(0, mindone_lib_path)

# TODO: mindone support AutoModelForCausalLM
from transformers import AutoModelForCausalLM
from janus.models import MultiModalityCausalLM, VLChatProcessor
from janus.utils.io import load_pil_images, set_model_param_dtype


# ms 
mode = 1
ms.set_context(mode=mode)
if mode == 0:
    ms.set_context(jit_config={"jit_level": "O0"})


# specify the path to the model
# model_path = "deepseek-ai/Janus-Pro-7B"
model_path = "ckpts/Janus-Pro-1B"
vl_chat_processor: VLChatProcessor = VLChatProcessor.from_pretrained(model_path)
tokenizer = vl_chat_processor.tokenizer

# TODO: support setting FA. currently can set in modeling_vlm.py
vl_gpt: MultiModalityCausalLM = AutoModelForCausalLM.from_pretrained(
    model_path, trust_remote_code=True,
    # use_flash_attention_2=True,
)
vl_gpt = set_model_param_dtype(vl_gpt, ms.bfloat16)
vl_gpt.set_train(False)

question = 'explain this meme'
image = 'images/doge.png'

conversation = [
    {
        "role": "<|User|>",
        "content": f"<image_placeholder>\n{question}",
        "images": [image],
    },
    {"role": "<|Assistant|>", "content": ""},
]

# load images and prepare for inputs
pil_images = load_pil_images(conversation)
prepare_inputs = vl_chat_processor(
    conversations=conversation, images=pil_images, force_batchify=True
).to(ms.bfloat16)  # NOTE: no device, all inputs are bf16 

# # run image encoder to get the image embeddings
inputs_embeds = vl_gpt.prepare_inputs_embeds(**prepare_inputs)

# run the model to get the response
# TODO: support kv cache 
# FIXME: can't set max_new_tokens=512 due to the limitation in mindone/transformers/generation/utils.py L734
outputs = vl_gpt.language_model.generate(
    inputs_embeds=inputs_embeds,
    attention_mask=prepare_inputs.attention_mask,
    pad_token_id=tokenizer.eos_token_id,
    bos_token_id=tokenizer.bos_token_id,
    eos_token_id=tokenizer.eos_token_id,
    max_new_tokens=1024,
    do_sample=False,
    use_cache=True,
    # use_cache=False,
)

answer = tokenizer.decode(outputs[0].asnumpy().tolist(), skip_special_tokens=True)
print(f"{prepare_inputs['sft_format'][0]}", answer)
