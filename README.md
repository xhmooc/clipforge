# 切片工坊 ClipForge · 电商直播切片工作台

把一场直播回放（几小时的 FLV/MP4）变成一批可以直接发布的竖屏切片：

**导入回放 → 转写字幕 → 编辑字幕 → AI 找商品讲解段 → 框出片段 → 加字幕样式/贴纸/镜像 → 导出 9:16 成片**

整套东西跑在本地：后端是个 FastAPI 服务，前端是 React 构建出来的静态页，由后端同一个端口托管，浏览器打开 `http://127.0.0.1:8000` 就能用。没有云端存储，素材和中间产物全部落在本地工程目录里。

---

## 一、跑起来

### 1. 后端

```bash
cd clipforge
python -m venv .venv
.venv\Scripts\activate
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

`imageio-ffmpeg` 自带 ffmpeg 二进制，**不需要单独装 ffmpeg**。如果系统 PATH 里有 ffmpeg，会优先用它。

### 2. 前端

```bash
cd frontend
npm install
npm run build
```

构建产物落在 `frontend/dist`，后端启动时如果发现这个目录存在，就自动挂到 `/` 上。开发时也可以 `npm run dev`，Vite 会代理到 8000。

### 3. 打开

浏览器访问 <http://127.0.0.1:8000>。首次使用先去右上角「设置」把模型填上（见下一节）。

---

## 二、模型从哪来

**模型文件不进仓库**（体积大）。按下面两条路自己准备，然后填进设置。

### 转写：本地 llama.cpp 跑 Qwen3-ASR（推荐，离线可用）

1. 去 [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) 下 **Windows CPU 版**：`llama-bXXXX-bin-win-cpu-x64.zip`（有显卡就下对应的 cuda/vulkan 版），解压到：

   ```
   vendor/llama/            ← 里面应该有 llama-server.exe
   ```

2. 去 [ggml-org/Qwen3-ASR-0.6B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF) 下这两个文件，放进：

   ```
   vendor/models/Qwen3-ASR-0.6B-Q8_0.gguf              约 767 MB
   vendor/models/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf       约 205 MB
   ```

   两个都要：`mmproj` 那个是**音频编码器**，缺了它就只认识字、不认识声音。

3. 设置 →「语音识别」→ 走哪条路选 **本地 llama.cpp**。路径留空就用上面这两个默认位置，也可以自己填别的。

**不需要手动起服务**：第一次点「转写」时后端会自己把 `llama-server` 拉起来（`127.0.0.1:8081`），之后常驻复用，后端退出时自动收掉。实测约 **0.4 倍实时**（40 秒音频 ≈ 18 秒）。

### 转写备用：云端接口

设置 →「语音识别」→ 走哪条路选「云端接口」，填任意 OpenAI 兼容网关的 `base_url` / `api_key` / 模型名即可。网关至少要支持 `/v1/chat/completions` 里带 `input_audio` 的音频输入。

### 找高光 / 起标题：文本模型

设置 →「文本模型」，同样填一个 OpenAI 兼容的 `base_url` / `api_key` / 模型名。

> 这一步目前**必须联网**，是整套流程里唯一还需要云端的部分。想做到 100% 离线，可以再起一个 llama.cpp 实例跑一个小号 instruct 模型（换个端口，别和 8081 撞），把这里的地址指过去。

### 设置存在哪

仓库里带了一份模板 [`config.example.json`](config.example.json)，里面列着**所有可以填的项**。

- 想直接改文件：把它复制成 `config.json` 再填。
- 或者完全不管它，启动后在「设置」里填也一样——两种方式效果相同，程序会自己生成 `config.json`。

`config.json` 本身被 `.gitignore` 排除（它会存下你的 API Key、本机路径和导出目录），所以**别把它提交到仓库里**。

---

## 三、目录结构

```
clipforge/
├── backend/
│   ├── main.py               FastAPI 入口：路由 + 后台任务
│   ├── models.py             Pydantic 数据模型（Project / Clip / Sticker / SubtitleCue …）
│   ├── config.py             设置读写（config.json），密钥脱敏回显
│   ├── subtitles.py          SRT 读写（对用户可见的可编辑中间产物）
│   └── services/
│       ├── project.py        workspace 下的工程管理
│       ├── ffmpeg.py         ffmpeg 封装：探测、抽音频、预览策略、切片渲染、拼接
│       ├── asr.py            转写流水线：停顿检测切块 → 并行请求 → 文字对齐到时间轴
│       ├── cloud.py          OpenAI 兼容网关客户端（音频/对话）
│       ├── llama_asr.py      本地 ASR：按需拉起 llama-server 并调它的接口
│       ├── ass.py            生成 ASS 字幕（位置 \pos、按框宽折行）
│       ├── fonts.py          从 ttf/otf 里读字体族名（libass 按族名找字体）
│       ├── highlight.py      LLM 读转写稿，找商品讲解段
│       └── render.py         出片：切片段、烧字幕、贴贴纸、镜像、拼接预览
├── frontend/
│   ├── src/App.jsx           整个工作台（单文件组件）
│   ├── src/api.js            接口封装
│   ├── src/styles.css
│   └── vite.config.js
├── vendor/                   llama.cpp 二进制 + GGUF（不入库，自己下）
├── workspace/                每个工程一个目录（不入库）
├── config.example.json       配置模板（入库，抄一份来改）
└── config.json               本机设置（不入库）
```

### 一个工程目录长什么样

```
workspace/<工程 id>/
├── source.mp4          原始回放（保持上传时的扩展名）
├── preview.mp4         浏览器能播的预览（能直接播时不生成）
├── audio.wav           16k 单声道，喂给 ASR
├── subtitles.srt       字幕：独立、可编辑的中间产物
├── subtitles.raw.srt   ASR 原稿
├── clip_001.ass        渲染某个片段时用的字幕（按片段内时间轴平移过）
├── stickers/           上传的贴纸图片
├── fonts/              上传的字体
├── renders/            导出的成片
├── _preview_parts/     拼接预览的分段
├── concat_preview.mp4  所有片段串起来的预览
└── project.json        工程元数据
```

---

## 四、数据流

```
上传回放
   └─ 后台：探测分辨率/时长 → 准备预览（三级策略）
        1. 源就是 MP4/H.264 → 什么都不做，视频接口直接回退到原片
        2. FLV/TS 里装的是 H.264/AAC → 只换封装（-c copy），秒级
        3. 编码浏览器不认 → 才真的重新转码
   └─ 转写（可随时开始）
        抽音频 → 检测停顿 → 切块 → 并行调 ASR → 把文字对齐到检测出的停顿上
   └─ AI 找高光
        转写稿（带 [起始秒-结束秒] 锚点）交给 LLM → 返回商品讲解段 + 段内精选窗口
   └─ 人工编辑
        字幕：改文字/时间、在时间轴上拖断句、拖位置、改框宽
        片段：时间轴上拖拽（吸附 + 挤压）、在播放头处剪开、与下一段合并
        装饰：贴纸（可拖位置/大小/出现时间）、镜像
   └─ 导出
        每个片段单独渲染成 1080×1920，字幕按片段内时间轴平移后烧录
```

---

## 五、给后续开发者（含 AI 协作）的实现约定

这些是踩过坑之后定下来的规则，改代码时请一并遵守。

**字幕相关**

- **字幕必须是独立的可编辑中间产物**。`subtitles.srt` 始终存在且可改，烧录永远是最后一步；改字幕不能触发重新转写或整条重渲染。
- **导出切片时，片段内的字幕时间轴要整体减去片段起点**，否则字幕会错位（见 `render.py::_cues_for_clip`）。
- **断句靠音频停顿检测，不能按字数硬切**。按字数切会把一句话拦腰截断（"充绒量 / 很高" 这种）。见 `asr.py`。
- **字幕用自建 ASS 渲染，不用 SRT + `force_style`**。`force_style` 表达不了任意落点，而 ASS 每行可以写 `\pos`，所以字幕才能拖。`ass.py` 里 PlayRes 固定 **216×384**（成片 1080×1920 的 9:16 等比），字号、描边、位置全用这套坐标，预览和成片才会落在同一个地方。
  - ⚠️ 加了 `\pos` 之后 libass **不再自动折行**，所以折行是我们在 Python 里按显示宽度算的（汉字算 1、西文算 0.5）。折行预算 = **框宽 ÷ 字号**，`WrapStyle: 2` 只认显式 `\N`。
  - **字号和框宽是两个概念**：字号管字多大，框宽管一行放几个字。别把两者混成一个"缩放"。

**时间轴**

- 拖动**吸附**半径按**屏幕像素**算（8px），不按秒——放大看单句和缩着看全片的手感才一致。
- 拖片段时**不要吸附字幕断句**：一场直播上千条字幕，句边界密到处处都在吸附半径内，会一直"发黏"。
- 片段的**挤压**每次都从「按下鼠标那一刻的快照」重算全部位置，而不是在上一次结果上叠加——否则来回拖时邻居会被越推越远，回不去。
- 贴纸的时间窗（`start`/`end`，`end=0` 表示到片尾）要先换算成**片段内相对时间**，再写进 `overlay` 的 `enable` 表达式（见 `render.py::_stickers_for_clip`）。

**渲染**

- **竖屏源和横屏源走两条链**：横屏用「模糊背景铺满 + 居中正片」，竖屏直接裁满画布（省掉整条 gblur）。`probe_size()` 在导入时判定。
- 滤镜链一律用**相对文件名 + cwd 设成工程目录**跑，绕开 Windows 上 `subtitles` 滤镜的路径转义问题。
- 模糊是在**缩小后的图**上做的再放大回来，比直接对 1080p 做高斯模糊便宜得多。

**并发与状态**

- 转写和渲染都是后端的后台任务，前端靠轮询 `project.status` 等结果。
- ⚠️ **后台任务不要用「开始时读到的工程快照」整体写回**。导入后立刻点转写是常见操作，预览转码任务如果拿旧快照写回，会把转写写进去的状态和字幕整个盖掉——`_job_prepare` 现在只更新它自己拥有的字段（时长/分辨率/方向/预览文件/提示语）。

**本地 ASR**

- 走 llama.cpp 而不是 transformers，原因是**提交量**：llama.cpp 用 mmap 加载 GGUF，权重只算文件映射页，几乎不计入 Windows 的「提交限额（物理内存 + 页面文件之和）」；transformers 会把整份权重提交进进程私有内存，加载时还要再复制一份。页面文件被固定得很小的机器上，transformers 那条路会直接被顶穿（`os error 1455`）。

---

## 六、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/PUT | `/api/settings` | 读写设置（密钥脱敏回显，回传掩码不会覆盖真密钥） |
| POST | `/api/projects` | 上传回放，建工程 |
| GET | `/api/projects` · `/api/projects/{id}` | 工程列表 / 单个工程 |
| POST | `/api/projects/{id}/transcribe` | 开始转写 |
| PATCH | `/api/projects/{id}/subtitles` | 保存字幕 |
| PATCH | `/api/projects/{id}/clips` | 保存片段 |
| PATCH | `/api/projects/{id}/style` | 保存字幕样式与位置（字体/字号/颜色/x/y/框宽） |
| POST | `/api/projects/{id}/stickers` · PATCH · DELETE | 上传 / 调整 / 删除贴纸 |
| POST | `/api/projects/{id}/fonts` | 上传字体（自动解析族名） |
| POST | `/api/projects/{id}/analyze` | AI 找高光（可带 `limit` 段数） |
| POST | `/api/projects/{id}/render` | 导出所有片段 |
| POST | `/api/projects/{id}/preview` · GET `/concat` | 拼接预览 |
| GET | `/api/projects/{id}/video` · `/renders/{name}` · `/stickers/{name}` | 取媒体文件 |

---

## 七、已知限制 / 待办

- **转写的时间戳是估算的**：ASR 只回文字，时间轴靠「静音检测 + 按字数占比分配」推出来，不是逐字时间戳。想更准需要接入带时间戳的引擎或对齐模型。
- **拼接预览会重新渲染一遍所有片段**（`_preview_parts/`），片段多或长时比较慢。
- 时间轴只有**片段轨是单轨**——因为挤压保证片段之间不重叠，多轨没有意义。如果以后要允许片段重叠，需要再改。
- **贴纸轨有 3 条泳道**，可以上下拖换道；改泳道数量要同步改 `App.jsx` 的 `STICKER_LANES` 和 CSS 里的轨高。
- 片段的**边缘拖动不参与挤压**（只有整段平移会顶开邻居）。
- 面板尺寸、时间轴缩放存在浏览器 localStorage，换浏览器要重设。
