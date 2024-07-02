#  Stable Video 3D (SV3D)

<p align="center"><img width="600" alt="Output Vis"
src="htmlthatcreatebygithub"/></p>

## Introduction

Stable Video 3D (SV3D) is a latent video diffusion model for high-resolution, image-to-multi-view generation of orbital videos around a 3D object. It adapts image-to-video diffusion model for novel multi-view synthesis and 3D generation, thereby leveraging the generalization and multi-view consistency of the video models, while further adding explicit camera control for novel view synthesis. An improved 3D optimization techniques was adopted to use SV3D and its NVS outputs for image-to-3D generation.

___Notice that the NeRF part for 3D optimization is not released yet. The current config setup for sv3d_u is based on a finetuned version of SVD with a decoder change.___ 

<p align="center"><img width="600" alt="SV3D Pipeline Arch"
src="htmlthatcreatebygithub"/>
<br><em>An example of a single U-Net Block with Added Temporal Layers (for more information please refer to <a href="#acknowledgements">[2]</a>)</em></p>

## Pretrained Models
- [ ] Mindspore Checkpoint Release

## Inference

```shell
python simple_video_sample.py --config configs/sv3d_u.yaml \
--ckpt PATH_TO_CKPT \
--image PATH_TO_INPUT_IMAGE
```

## Acknowledgements

1. Voleti, Vikram, Chun-Han Yao, Mark Boss, Adam Letts, David Pankratz, Dmitry Tochilkin, Christian Laforte, Robin Rombach, and Varun Jampani. "Sv3d: Novel multi-view synthesis and 3d generation from a single image using latent video diffusion."
