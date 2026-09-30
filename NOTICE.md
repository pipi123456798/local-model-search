# 第三方组件与法律说明

本文件说明「本地模型检索」项目中使用到的第三方组件、分发内容与免责声明。

## 第三方开源组件

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| [three.js](https://threejs.org/) 0.160.1（含 OrbitControls、GLTFLoader） | 浏览器端 3D 预览渲染 | MIT |
| [PyTorch](https://pytorch.org/) | 形状特征推理运行时 | BSD-3-Clause |
| [NumPy](https://numpy.org/) / [SciPy](https://scipy.org/) | 数值与几何计算 | BSD-3-Clause |
| [trimesh](https://trimesh.org/) | 网格加载与处理 | MIT |
| [einops](https://github.com/arogozhnikov/einops) | 张量操作 | MIT |
| [Flask](https://flask.palletsprojects.com/) | 本地服务框架 | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | 图像处理（预览生成） | MIT-CMU |
| [lxml](https://lxml.de/) | XML 解析（3MF 等格式） | BSD-3-Clause |
| [cadquery-ocp-novtk / OCP](https://github.com/CadQuery/OCP/blob/7.9.3.1/LICENSE) | OCCT 的 Python 绑定 | Apache-2.0，Copyright 2020 OCP contributors |
| [Open CASCADE / OCCT](https://github.com/Open-Cascade-SAS/OCCT) | STEP 解析及三角化内核 | LGPL-2.1 与 OCCT 例外条款；不受 OCP 的 Apache-2.0 标签覆盖 |
| [pywebview](https://github.com/r0x0r/pywebview) | 独立桌面窗口 | BSD-3-Clause |
| [Shape](https://github.com/simd-ai/shape) / [Small v3 权重](https://huggingface.co/bayang/shape-foundation-small-v3) | 形状特征提取 | Apache-2.0 |

three.js 许可证全文见 `app/web/model_search/THREE-LICENSE.txt`；
完整 Python 依赖清单见 `requirements.txt`，发布构建版本见 `packaging/requirements-build.txt`。
安装包的 `licenses/` 保留所收集依赖的许可证、METADATA 和版本信息，各组件版权归其各自作者所有。
本项目根目录 MIT License 仅适用于本项目原创部分，不替代第三方组件的许可证。

OCP/OCCT 以可替换的动态库形式分发；本项目不限制用户依其许可证进行替换、修改或为调试这些修改进行逆向工程。
Python 分发包版本见安装包 `licenses/versions.json`，这不等于所有原生 DLL 的版本清单。
`cadquery-ocp-novtk 7.9.3.1.1` 的[固定构建工程](https://github.com/CadQuery/ocp-build-system/tree/df2c31c25b8fce57c497895aa514e9c3550c9f02)
使用 OCCT `V7_9_3`，并应用工程内的 CMake 补丁。其 Windows / Mac 配方从 conda 安装
`freeimage=3.18.*`、`freetype=2.12.*`、`fontconfig=2.13.*`，没有固定这些传递原生依赖的完整构建号。

**发布核查状态**：OCP 自身的 Apache-2.0 已核实；wheel 还携带 FreeImage、LibRaw、OpenEXR 等独立组件。
完整原生许可证、精确构建来源、补丁和所需对应源码材料尚未补齐，当前本地安装包仅用于验收，不作为已完成再分发审核的公开资产。
上述上游链接和自动收集的 METADATA 不是完整的对应源码交付或许可审核结论。

## 权重包（shape-kit.zip）说明

- **内容**：形状特征提取网络的代码（`shape_foundation/`）与推理权重
  （`checkpoints/checkpoint_final.pt`），以及 CAD 解析工具 `step_utils.py`。
- **来源**：Shape Foundation Model，Notelink LLC / simd-ai；权重发布者 bayang。上游代码基准为 `4f632ca4b283ec28de98c1434598ddd5d21c8cbb`。
- **许可**：代码仓库 LICENSE 与模型卡 `license: apache-2.0` 均声明 Apache-2.0；完整许可证随 `kit/LICENSE` 分发。
- **核验**：本地 `checkpoint_final.pt` 的 SHA-256 为 `a7f4ebd478f972312a1e4b4e9c510c6592fcc91f0901d6c1ebe1cb68bb6c6d53`，与模型仓库 LFS 记录一致。
- **证据**：上游代码与模型仓库链接见上表；2026-09-30 模型卡及 LFS 信息因官网直连超时经 hf-mirror.com 镜像读取，并以官方网页搜索索引交叉核对。机器可读记录见 `packaging/shape-manifest.json`。
- **本地修改**：仅纳入推理子集；精简 `shape_foundation/`、`configs/`、`data/`、`models/`、`preprocessing/` 的初始化导入；`models/tokenizer_magno.py` 保留本地推理适配。修改文件含明确声明，仍按 Apache-2.0 分发。
- **分发**：安装包内置；源码启动方式单独下载带校验的权重。模型权重不作修改，不分发训练数据或上游数据集的预计算嵌入。
- **用途**：仅用于在本机提取 3D 模型的几何形状特征（推理），不包含任何用户数据。

## 隐私说明

- 所有计算（特征提取、检索、预览）均在本机完成；
- 本地服务仅监听 `127.0.0.1`（回环地址），并使用随机会话令牌；
- 完整安装包内置运行依赖和权重，下载安装包后可离线使用；源码启动方式仍需联网准备依赖。

## 商标与隶属关系

- “Bambu Studio”、“Bambu Lab”等名称与商标归其各自权利人所有；
- 本项目为独立的社区工具，**与 Bambu Lab 无任何隶属、合作或背书关系**；
- 本项目是独立检索软件，不包含完整 Bambu Studio 切片程序，也不宣称第三方对本项目提供背书。
