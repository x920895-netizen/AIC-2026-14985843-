# 校园智服启动与复现

## 本机当前环境

也可直接双击根目录`启动校园智服.cmd`，再在浏览器打开本机地址。若当前服务已经运行，直接打开页面即可，不要重复启动占用同一端口。

在项目根目录运行：

```powershell
.venv/Scripts/python.exe -B -X utf8 scripts/run_campus.py
```

浏览器打开`http://127.0.0.1:8765`。脚本会发现独立安装在`.tools/campus-app`的网页依赖；默认离线，无API Key也可运行。关停在终端按Ctrl+C。状态保存在`state/campus_app`，重开继续读取，不覆盖历史实验。

新的一次演示使用新目录：

```powershell
.venv/Scripts/python.exe -B -X utf8 scripts/run_campus.py --state state/campus_recording_01 --port 8765
```

若端口已有服务，先关停或选择8766。完整离线自动演示要求没有候选历史；应用不会为演示清空旧历史。

## 干净环境

Python3.12创建独立虚拟环境后安装`requirements-app.txt`即可启动应用。校园路径的历史评分模块只在Hotpot测试时导入DSPy，普通应用不需要安装研究全依赖。完整旧研究测试使用原`.venv`，新环境若要运行这些测试则另按`requirements.txt`安装全部研究依赖。

```powershell
python -m venv .venv-app
.venv-app/Scripts/python.exe -m pip install -r requirements-app.txt
.venv-app/Scripts/python.exe -B -X utf8 scripts/run_campus.py
```

不要把`.tools`、虚拟环境、状态库、缓存或密钥上传为公开源码。

## 真实模式

仅账号持有人确认当前模型、服务地址与费率后，在本地设置环境变量DASHSCOPE_API_KEY（不要把值写入文件或录屏），运行：

```powershell
.venv/Scripts/python.exe -B -X utf8 scripts/run_campus.py --allow-real --state state/campus_real_01
```

打开页面切换真实模式，每次有费用的操作弹出确认。配置见[[campus_app/config.json|应用配置]]，默认总预算¥2、最多80次，父子验证总额门禁¥0.5。提议最多先运行12题再进行1次反思；验证运行12+12题，单次留出12题。真实模式不会自动回退为离线。服务费率为历史参考，未验证当前可用性。改变模型、数据或配置必须使用新状态目录，以保持版本冻结一致。

## 自动化检查

```powershell
.venv/Scripts/python.exe -B -X utf8 scripts/verify_campus.py
```

实际报告写入`output/campus-delivery/test-results.json`与`test-log.txt`。请求替身不连接外部模型，测试中的调用数字为替身执行次数。[[docs/campus-delivery-status.md|交付状态]]说明已执行检查与未执行范围。核心数据和协议见[[docs/campus-evaluation-protocol.md|评价协议]]；接口输入schema见本机`/api/docs`。
