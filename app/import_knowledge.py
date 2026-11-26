"""Import explicitly supplied local cognition exports. Never scans private folders."""
import argparse
import json
import os
from pathlib import Path
from db import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('files',nargs='+',type=Path)
    args=parser.parse_args()
    store=Store(Path(os.getenv('IP_DATA_DIR',str(Path(__file__).resolve().parent/'.runtime'))))
    reports=[]
    for file in args.files:
        if file.stat().st_size>1024*1024:raise ValueError('导入文件超过 1MB。')
        text=file.read_text(encoding='utf-8-sig')
        reports.append({'filename':file.name,**store.import_brain(text,file.name)})
    print(json.dumps({'imports':reports,'knowledge_count':len(store.list('knowledge',10000))},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
