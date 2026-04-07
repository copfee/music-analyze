# WAV 真无损鉴别报告工具

使用 Python 3 扫描指定目录下的 PCM WAV 文件，读取容器元数据，并基于频谱截止估算音频的实际有效采样率，输出一个自包含的 HTML 报告，用于辅助识别疑似伪无损文件。

## 功能

- 递归扫描目录及子目录中的 `.wav` 文件
- 读取文件容器采样率、采样位深
- 对音频内容做频谱分析，估算实际有效采样率
- 标记 `正常`、`疑似伪无损`、`非 PCM / 无法分析`
- 在命令行输出处理日志和当前进度
- 输出单文件 HTML 报告

## 使用 uv 管理依赖

本项目使用 `uv` 管理 Python 依赖。

如果尚未安装 `uv`，先安装：

```bash
pip install uv
```

或参考官方方式安装：

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装依赖：

```bash
uv sync
```

这会根据 [pyproject.toml](C:\projects\music-analysis\pyproject.toml) 安装项目依赖，目前包含：

- `numpy`

## 运行方式

基础命令：

```bash
uv run python analyze_wav.py --input <目录>
```

也可以直接通过项目脚本入口运行：

```bash
uv run analyze-wav --input <目录>
```

Windows 示例：

```bash
uv run analyze-wav --input "D:\Music"
```

默认输出到输入目录下的：

```text
wav-analysis-report.html
```

如果需要自定义输出路径，仍然可以显式指定：

```bash
uv run analyze-wav --input "D:\Music" --output "D:\Reports\music-report.html"
```

## 参数

- `--input`
  - 必填，待扫描的根目录
- `--output`
  - 可选，输出的 HTML 文件路径
  - 默认值为 `<输入目录>/wav-analysis-report.html`
- `--threshold-db`
  - 频谱有效能量阈值，默认 `-55`
- `--min-ratio`
  - 某频率成分在所有分析窗口中出现的最小比例，默认 `0.12`
- `--max-seconds`
  - 每个文件最多参与分析的总时长，默认 `30`
- `--window-size`
  - FFT 窗口大小，默认 `8192`
- `--log-level`
  - 日志级别，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`
  - 默认 `INFO`

完整示例：

```bash
uv run analyze-wav \
  --input "D:\Music" \
  --threshold-db -55 \
  --min-ratio 0.12 \
  --max-seconds 30 \
  --window-size 8192 \
  --log-level INFO
```

## 日志与进度

运行时会在终端输出：

- 阶段日志
  - 例如开始扫描、发现文件数、当前分析文件、报告写出、最终统计
- 单行进度条
  - 显示当前处理数量、总文件数和正在处理的文件名

如果需要更详细的调试信息：

```bash
uv run analyze-wav --input "D:\Music" --log-level DEBUG
```

## 报告字段

HTML 报告包含以下字段：

- `文件目录`
  - 相对于输入根目录的目录路径
- `文件名`
  - WAV 文件名
- `文件采样率`
  - WAV 容器头中的采样率
- `实际采样率（估算）`
  - 基于频谱最高有效带宽估算出的采样率
- `规格`
  - 当 WAV 容器采样率严格大于 `48000 Hz` 时显示 `高解析`，否则显示 `-`
- `采样比特`
  - WAV 中的位深
- `状态`
  - `正常`、`疑似伪无损`、`非 PCM / 无法分析`
- `说明`
  - 当前结果的补充说明

## 判定说明

`文件采样率` 和 `实际采样率（估算）` 不是同一个概念：

- `文件采样率` 来自 WAV 文件头
- `实际采样率（估算）` 来自音频频谱中的最高有效频率，再乘以 2 得到
- `规格` 仅根据 `文件采样率` 判断；当容器采样率严格大于 `48000 Hz` 时标记为 `高解析`

例如：

- 文件头显示 `96000 Hz`
- 但频谱有效内容只延伸到约 `22000 Hz`
- 则估算实际采样率可能接近 `44100 Hz` 或 `48000 Hz`

因此：

- `96000 Hz`、`88200 Hz`、`192000 Hz` 会显示 `高解析`
- `48000 Hz`、`44100 Hz` 不会显示 `高解析`

这类文件通常是低采样率音频升采样后重新封装，可能属于疑似伪无损。

注意：

- 这是工程上的筛查工具，不是绝对准确的司法级鉴定工具
- 某些录音、母带处理、低通滤波、静音片段较多的音频，可能影响估算结果

## 支持范围

当前版本支持：

- PCM WAV
- `WAVE_FORMAT_EXTENSIBLE` 且子格式为 PCM 的 WAV

当前版本不保证支持：

- MP3
- FLAC
- AAC
- 非 PCM 编码 WAV

遇到不支持或损坏的文件时，报告会显示 `非 PCM / 无法分析`，不会中断整个任务。

## 输出说明

输出结果是一个自包含的 HTML 文件，不依赖外部 CSS 或 JS。直接用浏览器打开即可查看。

## 项目文件

- [analyze_wav.py](C:\projects\music-analysis\analyze_wav.py)
  - 主脚本，负责扫描、分析和生成报告
- [pyproject.toml](C:\projects\music-analysis\pyproject.toml)
  - `uv` 依赖管理和项目入口配置

## 实现流程

1. 递归扫描 WAV 文件
2. 读取 `fmt` 信息判断是否为 PCM
3. 读取音频样本并转为单声道分析
4. 对多个窗口执行 FFT
5. 统计达到阈值的最高有效频率
6. 生成 HTML 报告
