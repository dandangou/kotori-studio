# 第三方组件与素材

本仓库发布应用源码，不附带第三方二进制、模型权重、直播视频或真人语音样本。

- Python 依赖见 `requirements.txt`；每个包遵循其自身许可证，安装时从包仓库获取。
- [FFmpeg](https://ffmpeg.org/legal.html) / FFprobe：安装脚本从 [OSXExperts](https://www.osxexperts.net/) 下载 Apple Silicon 构建，并校验固定 SHA256。构建许可证和对应源码以该发行方说明为准；重分发二进制前应单独核查。
- [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) 与 [MLX-LM](https://github.com/ml-explore/mlx-lm) 是本地推理组件。
- Whisper 与 Qwen 的模型权重按需单独下载；各模型仓库中的许可与模型说明适用。使用应用源码的许可不代替模型许可。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 用于用户主动请求的视频下载。
- macOS 字体和系统合成语音由操作系统提供，没有复制到本仓库。
- 用户导入的视频、字幕、角色名称及相关作品的权利归各自权利人。软件许可不授予转载视频、音乐或角色素材的权利。
