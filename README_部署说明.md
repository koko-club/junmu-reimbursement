# 差旅报销单网页工具部署说明

本目录（或同目录的 `差旅报销单网页工具-部署包-YYYYMMDD.zip`）是一套本地网页工具：在浏览器填写表单，提交后生成 Excel 和 PDF。生成文件保存在 `generated` 文件夹。

## 一、macOS 直接运行（推荐）

默认部署包带有 macOS Apple Silicon（arm64）版 Python 3.12 和无界面 LibreOffice，通常不需要另外安装 Python 或 LibreOffice。

1. 将 zip 解压到一个完整目录，不要只拷贝其中某个文件。
2. 双击 `start_reimbursement_tool.command`。
3. 如果 macOS 第一次阻止运行：右键该文件，选择“打开”；仍被阻止时，到“系统设置 -> 隐私与安全性”允许打开。
4. 浏览器会打开本机地址 `http://127.0.0.1:端口`。

脚本会优先使用包内 `runtime`；如果删除了 `runtime`，则会退回到系统 `python3`，这时需先执行：

```sh
python3 -m pip install -r requirements.txt
```

并确保系统中有 LibreOffice。可从 <https://www.libreoffice.org/download/download/> 安装。

## 二、让同一局域网的朋友访问

只在可信的家庭/办公室局域网使用。当前工具没有登录和权限控制，能连到端口的人都可以提交并下载文件。

1. 用文本编辑器打开 `config.json`。
2. 将 `host` 改为 `0.0.0.0`，将 `port` 改为一个固定端口，例如 `8000`：

```json
{
  "template_path": "resources/差旅报销单模板.xlsx",
  "output_dir": "generated",
  "host": "0.0.0.0",
  "port": 8000,
  "soffice_path": ""
}
```

3. 在运行工具的电脑上启动 `start_reimbursement_tool.command`。终端会打印 `LAN: http://...` 地址。
4. 把该 `LAN:` 地址发给朋友；他们与主机连接同一个 Wi-Fi/网线后，在浏览器打开即可。
5. 若 macOS 防火墙弹窗询问是否允许 Python 接收网络连接，请选择允许。也可以在“系统设置 -> 网络 -> 防火墙”中放行该程序。

主机电脑关闭启动窗口后，网页和下载都会停止。`ThreadingHTTPServer` 支持多个浏览器同时提交；文件名冲突会自动生成 `-2`、`-3` 后缀，不会覆盖已有文件。

## 三、Windows / Linux

便携运行时是 macOS arm64 专用。Windows 或 Linux 请使用同一项目的“无运行时”部署包，或在本目录执行 `build_deployment_package.py --without-runtime` 生成：

1. 安装 Python 3.10 或更高版本。
2. 安装 LibreOffice，并确认 `soffice` 在 PATH 中；Windows 也可以把 `config.json` 的 `soffice_path` 改成 `soffice.exe` 的完整路径。
3. 在项目目录打开终端执行：

```sh
python -m pip install -r requirements.txt
python app.py
```

Windows 也可双击 `start_reimbursement_tool.bat`。Linux/macOS 终端可执行 `./start_reimbursement_tool.sh`。

## 四、重新打包

在当前项目目录执行：

```sh
python3 build_deployment_package.py
```

默认生成带 macOS arm64 运行时的完整包到 `dist/`。如果只需要代码和网页资源：

```sh
python3 build_deployment_package.py --without-runtime
```

模板位于 `resources/差旅报销单模板.xlsx`，换模板时请保持这个文件名，或同步修改 `config.json` 的相对路径。不要把个人生成结果放进部署包；`generated` 目录只在运行时保存输出文件。

