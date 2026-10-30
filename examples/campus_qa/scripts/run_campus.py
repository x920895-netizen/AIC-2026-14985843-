"""启动校园缓考助手；默认离线，仅监听127.0.0.1。"""
import argparse
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]          # examples/campus_qa
REPO=Path(__file__).resolve().parents[3]          # 开源包根目录（提供 harness 包）
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(REPO))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--state',type=Path,default=ROOT/'state/campus_app')
    parser.add_argument('--allow-real',action='store_true',help='允许页面逐次确认后付费调用，启动本身不发送请求')
    args=parser.parse_args()
    import uvicorn
    from campus_app.api import create_app
    app=create_app(args.state,allow_real=args.allow_real)
    print(f'校园智服：http://127.0.0.1:{args.port}；默认离线。模拟政策不代表真实高校规定。',flush=True)
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
