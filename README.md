# B站视频转文档（bilibili-to-doc）

把 Claude Code skill [`bilibili-to-doc`](https://github.com/programmerloverun/bilibili-to-doc) 封装成的**独立本地程序**：双击桌面快捷方式 → 自动打开本地网页 → 粘贴 B 站视频链接 → 一键下载 AI 字幕、调用你配置的 AI 模型整理成结构化 Markdown 文档，并保存到桌面。

## 快速开始

### 本机已部署

双击桌面的 **「B站视频转文档」** 快捷方式即可使用，流程见下。

### 从仓库克隆（新用户）

1. 安装 Python 3.10+（安装时勾选 *Add python.exe to PATH*），克隆本仓库。
2. 双击 `install-shortcut.bat` → 桌面生成「B站视频转文档」快捷方式（不想建快捷方式也可直接双击 `start.bat`）。
3. 首次启动会自动安装依赖 `yt-dlp`（需联网）。
4. 网页打开后，点击输入框**右上角的 ⚙ 设置按钮**，配置**你自己的**：
   - 🍪 **B站 Cookies**：导入 cookies.txt 文件 / 粘贴 Cookie 文本 / 从浏览器读取（B 站 AI 字幕通常需要登录态）。
   - 🤖 **AI 模型 API**：填写任意 OpenAI 兼容接口的 Base URL、API Key、模型名称（如 DeepSeek、OpenAI、Moonshot、通义等），可点「测试连接」验证。
   - 📁 **保存**：设置文档保存目录（默认桌面）。

> 为什么 Cookies 和 API Key 要自己填：它们是个人凭据，仓库里不包含任何作者的配置
> （`config.json` / `data/` 被 `.gitignore` 排除，首次运行自动生成）。

5. 在输入框粘贴 B 站视频链接（如 `https://www.bilibili.com/video/BV1xxxxxxxxxx`），点击 **生成文档**。
6. 完成后页面会预览生成的 Markdown，可下载、复制或直接打开所在文件夹。

## 启动与停止

- **启动**：双击桌面快捷方式即可启动（只会启动一个实例；重复双击只会再次打开网页，不会重复启动）。
- **停止**：**关闭那个命令行窗口**，程序立即停止；或点击网页底部左侧的 **⏹ 停止程序** 按钮，效果相同。
- 只关闭浏览器网页不会停止程序；此时再次双击快捷方式可重新打开网页。

## 工作原理

```
B站视频链接
  ├─► yt-dlp（+ Cookies）下载 AI 中文字幕（ai-zh / zh-Hans）
  ├─► 解析 SRT：去掉时间戳与编号，合并字幕文本
  ├─► 调用用户配置的 AI 模型 API（OpenAI 兼容 /chat/completions）
  │     按模板重组为结构化文档：章节标题、代码块、表格、要点总结
  └─► 保存 {视频标题}.md 到保存目录，并在网页预览
```

## 项目结构

```
bili2doc/
├── app.py                 # 本地 Web 服务（纯 Python 标准库 + yt-dlp）
├── web/index.html         # 网页前端（输入框 + 设置弹窗 + 结果预览）
├── start.bat              # 启动脚本（自动装依赖、双击即用；关窗口即停止）
├── start.vbs              # 隐藏窗口启动器（桌面快捷方式调用它）
├── install-shortcut.bat   # 双击在桌面创建快捷方式（内含 install-shortcut.ps1）
├── make_icon.py           # 生成 app.ico 图标
├── app.ico                # 应用图标
├── config.example.json    # 配置格式示例（真实配置运行时生成，不入库）
├── config.json            # 用户配置（运行时自动生成，被 .gitignore 排除）
├── data/cookies.txt       # 转换后的 Netscape 格式 Cookies（运行时生成，被排除）
└── app.log                # 运行日志（被排除）
```

## 环境要求

- Windows 10/11，Python 3.10+（需在 PATH 中，`start.bat` 会自动检测）
- yt-dlp：首次运行 `start.bat` 会自动安装；也可手动 `pip install -r requirements.txt`
- 浏览器（仅「从浏览器读取 Cookies」模式需要）

## 打包为安装程序（setup.exe）

生成免 Python 环境的安装包，需要：PyInstaller、Inno Setup 6（含中文语言文件）、官方 yt-dlp.exe。

```bash
pip install pyinstaller
# 1. 下载官方 yt-dlp.exe 到 build-tools\yt-dlp.exe
# 2. 冻结应用（web/ 目录自动打包进去）
python -m PyInstaller --noconfirm --clean --onedir --console --contents-directory . --name bili2doc --icon app.ico --add-data "web;web" app.py
# 3. 把 yt-dlp.exe 放进 dist\bili2doc\
copy build-tools\yt-dlp.exe dist\bili2doc\
# 4. 用 Inno Setup 编译 packaging\setup.iss → installer\bili2doc-setup-1.0.0.exe
ISCC.exe packaging\setup.iss
```

安装包特点：免 Python 环境、自动创建桌面快捷方式、关闭命令行窗口即停止；安装时自动从 `C:\common\bili2doc` 迁移已有配置（config.json / data），卸载时保留用户配置。

## 常见问题

- **提示"未能获取到字幕"**：先看报错里列出的可用字幕语言。若没有任何字幕，说明该视频本身没字幕，无法提取；若显示有 `ai-zh`/`zh-Hans` 等但提取失败，一般是 B 站风控间歇性拦截，程序会自动重试 3 次，稍等片刻或再点一次即可。B 站 AI 字幕需要登录态，请确认已在设置中导入有效的 B 站 Cookies（推荐浏览器扩展 *Get cookies.txt LOCALLY* 导出，或选择「从浏览器读取」）。
- **提示"HTTP 412"**：B 站风控拦截，与 Cookies 是否有效无关；程序会自动重试，或稍等几分钟再试。
- **提示"AI 接口返回错误"**：检查 API 地址（一般以 `/v1` 结尾）、Key 是否有效、模型名称是否正确、账户是否有额度。可用设置里的「测试连接」排查。
- **长视频生成失败**：调大「发送给 AI 的字幕最大长度」或换上下文更长的模型。
- **端口冲突**：默认使用 8787–8796 端口，被占用时自动切换；重复双击快捷方式不会重复启动服务。
- **桌面被 OneDrive 重定向**：程序自动识别真实桌面目录（如 `OneDrive\Desktop`）。

## 安全说明

- 服务只监听 `127.0.0.1`，不对外网开放；所有数据只在本机流转。
- API Key 与 Cookies 保存在本机 `config.json` / `data\` 目录下，请勿分享这些文件。
- 生成的文档末尾会标注"由 AI 自动生成"，AI 字幕可能存在少量识别错误。

## 发布到 GitHub / 数据安全

本项目已做好开源发布的防泄密处理，可直接推送：

- `config.json`（含 API Key）、`data/`（含 B 站 Cookies）已被 `.gitignore` 排除，永远不会进入仓库
- 运行日志 `app.log`、Python 缓存 `__pycache__/` 同样被排除
- 源码无任何硬编码密钥，所有配置运行时从本地 `config.json` 读取
- 新用户可参考 `config.example.json` 了解配置格式

```bash
git init -b main
git add .
git commit -m "initial commit"
gh repo create bilibili-to-doc --public --source=. --push
# 或手动：在 GitHub 创建仓库后
# git remote add origin https://github.com/<你的用户名>/bilibili-to-doc.git
# git push -u origin main
```

## 许可证

MIT © 2026 programmerloverun（沿用原 skill 的许可）
