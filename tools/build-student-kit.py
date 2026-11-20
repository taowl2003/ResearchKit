#!/usr/bin/env python3
"""Build an unconfigured Windows student kit with self-contained offline help.

Only allowlisted documentation, launchers and generic support files are used.
No server installation, workspace access or credential discovery takes place.
"""
from __future__ import annotations
import argparse
import hashlib
from html import escape, unescape
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import mistune

ROOT = Path(__file__).resolve().parents[1]
BUILD_DATE = '2026-11-20'
TOP = 'ResearchKit-学生上手包'
LAUNCHERS = ('Start-ResearchKit.cmd', 'Configure-ResearchKit.cmd', 'ResearchKit.ps1', 'ResearchKit-FirstUse.ps1')
DOCS = ('学生使用指南.md',)
ENTRY = '学生使用指南.html'
SUPPORT = ('launch.json', 'ssh-config.example', 'check-environment.py', 'requirements-basic.txt', 'requirements-cnn-extra.txt')
CSS = '''body{margin:0;background:#f1f5f9;color:#203246;font:17px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1050px;margin:24px auto;padding:24px 36px;background:#fff;border-radius:12px}h1,h2,h3{line-height:1.4;color:#163f60}h2{margin-top:2em;border-bottom:1px solid #dbe5ed;padding-bottom:8px}a{color:#126499}a:hover{text-decoration:none}.lead{font-size:21px}.notice{padding:16px;background:#eaf4fc;border-left:5px solid #2674aa}.links{display:flex;gap:16px;flex-wrap:wrap;margin:18px 0}.links a{padding:8px 12px;background:#edf4f8;border-radius:6px}pre{padding:16px;background:#142636;color:#e9f0f5;overflow:auto;line-height:1.5;font-size:14px}code{font-family:Consolas,monospace;overflow-wrap:anywhere}p code,li code{background:#edf2f7;padding:2px 4px}table{border-collapse:collapse;width:100%;margin:15px 0}td,th{border:1px solid #d8e1ea;padding:9px 12px;text-align:left}th{background:#edf4f8}details{border:1px solid #d5e1ec;border-radius:6px;padding:12px;margin:14px 0}summary{cursor:pointer;font-weight:600}blockquote{border-left:4px solid #86a3bc;margin-left:0;padding-left:16px;color:#456}nav{padding:12px 0;border-bottom:1px solid #ddd}footer{margin-top:36px;font-size:14px;color:#60758a}@media(max-width:650px){main{margin:0;padding:18px}body{font-size:16px}table{display:block;overflow:auto}}'''


def normalized_doc(text: str) -> str:
    """Rebase doc links without pretending project files live in this archive."""
    text = text.replace('../../docs/', '')
    text = text.replace('[`.vscode/launch.json`](.vscode/launch.json)', '`.vscode/launch.json`（本人项目内的配置文件）')
    text = text.replace('[`case.py`](case.py)', '`case.py`（本人项目入口）')
    # Encode parentheses in filenames before Markdown parsing; otherwise a
    # nested filename can be truncated into a broken relative link.
    for name in DOCS:
        text = text.replace('](' + name + ')', '](' + quote(name) + ')')
        text = text.replace('](' + name + '#', '](' + quote(name) + '#')
    return text


class Renderer(mistune.HTMLRenderer):
    def heading(self, text, level, **attrs):
        raw = unescape(re.sub('<[^>]+>', '', text)).lower()
        slug = re.sub(r'[^\w\s-]', '', raw).strip()
        slug = re.sub(r'\s+', '-', slug)
        return f'<h{level} id="{escape(slug)}">{text}</h{level}>\n'

    def link(self, text, url, title=None):
        parsed = urlsplit(url)
        if not parsed.scheme and parsed.path:
            path = unquote(parsed.path)
            if path.endswith('.md'):
                name = PurePosixPath(path).name
                if name not in DOCS:
                    raise ValueError('Unbundled document link: ' + path)
                url = quote(name[:-3] + '.html') + ('#' + parsed.fragment if parsed.fragment else '')
        return super().link(text, url, title)

    def block_code(self, code, info=None):
        result = super().block_code(code, info)
        if len(code.splitlines()) > 45:
            return f'<details><summary>展开完整代码（{len(code.splitlines())}行）</summary>{result}</details>\n'
        return result


def document_html(name, content):
    render = mistune.create_markdown(renderer=Renderer(escape=True), plugins=['table', 'strikethrough', 'url'])
    content = re.sub(r'<!-- (?:BEGIN|END) TUTORIAL: [\w-]+ -->\n?', '', content)
    body = render(content)
    headings = re.findall(r'<h([23]) id="([^"]+)">(.*?)</h\1>', body)
    navigation = '<nav class="links">'
    for level, slug, title in headings:
        if level == '2' and title.startswith(('第一部分','第二部分','第三部分')):
            navigation += f'<a href="#{slug}">{title}</a>'
    navigation += '</nav>'
    toc = '<details class="toc"><summary>展开全部章节目录（按需查找）</summary><ul>'
    for level, slug, title in headings:
        toc += f'<li class="level-{level}"><a href="#{slug}">{title}</a></li>'
    toc += '</ul></details>'
    # Contents and examples stay in one page, searchable with Ctrl+F offline.
    body = body.replace('</h1>', '</h1>' + navigation + toc, 1)
    return page(name, body, None)


def page(title, body, stylesheet):
    style = f'<link rel="stylesheet" href="{stylesheet}">' if stylesheet else '<style>' + CSS + '.toc .level-3{margin-left:1.2em}.toc li{margin:3px 0}pre{white-space:pre;tab-size:4}@media print{details{display:block}main{max-width:none}}</style>'
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title>' + style +
            '</head><body><main>' + body + f'<footer>ResearchKit学生上手包 · {BUILD_DATE} · 文档可离线阅读；连接和实验使用个人服务器账号。</footer></main></body></html>')


def outputs():
    files = {'启动器/'+name: (ROOT/'tools/windows'/name).read_bytes() for name in LAUNCHERS}
    content = normalized_doc((ROOT/'docs'/DOCS[0]).read_text())
    files[ENTRY] = document_html('ResearchKit 学生使用指南', content).encode()
    for name in SUPPORT:
        files['辅助文件/'+name] = (ROOT/'tools/student-kit'/name).read_bytes()
    files['辅助文件/PACKAGE.json'] = (json.dumps({'schema':'researchkit.student-kit/v2', 'built':BUILD_DATE,
        'compatible_platform':'ResearchKit Web v0.8 with IDE experiment artifacts',
        'entry':ENTRY, 'launcher':'启动器/Start-ResearchKit.cmd',
        'requires_existing_server_account':True,'includes_credentials':False,
        'installs_platform_server':False,'offline_docs':True,'offline_ide_or_python_installer':False,
        'source_documents':list(DOCS), 'launcher_sha256':{'启动器/'+n:hashlib.sha256(files['启动器/'+n]).hexdigest() for n in LAUNCHERS}},
        ensure_ascii=False,indent=2)+'\n').encode()
    files['辅助文件/SHA256SUMS'] = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name,data in sorted(files.items())).encode()
    return files


class Links(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.ids=set()
    def handle_starttag(self, tag, attrs):
        values=dict(attrs)
        if 'id' in values:self.ids.add(values['id'])
        for key in ('href','src'):
            if key in values:self.links.append(values[key])


def validate(files):
    parsed = {}
    for name,data in files.items():
        path=PurePosixPath(name)
        assert not path.is_absolute() and '..' not in path.parts
        assert path.name not in {'connection.json', 'authorized_keys', 'id_ed25519', 'id_rsa'}
        assert path.suffix not in {'.sqlite3','.blob','.pyc'}
        assert b'-----BEGIN OPENSSH PRIVATE KEY-----' not in data
        if name.endswith('.html'):
            parser=Links();parser.feed(data.decode());parsed[name]=parser
    for name,parser in parsed.items():
        for url in parser.links:
            link=urlsplit(url)
            if link.scheme or link.netloc:continue
            parts=list(PurePosixPath(name).parent.parts)
            for part in unquote(link.path).split('/'):
                if part in ('','.'):continue
                if part=='..':
                    assert parts, (name,url)
                    parts.pop()
                else:parts.append(part)
            target='/'.join(parts) if link.path else name
            assert target in files,(name,url,target)
            if link.fragment and target in parsed:
                assert unquote(link.fragment) in parsed[target].ids,(name,url,'missing fragment')
    for name in LAUNCHERS:
        assert files['启动器/'+name]==(ROOT/'tools/windows'/name).read_bytes()
    metadata=json.loads(files['辅助文件/PACKAGE.json'])
    for name,digest in metadata['launcher_sha256'].items():assert hashlib.sha256(files[name]).hexdigest()==digest
    for line in files['辅助文件/SHA256SUMS'].decode().splitlines():
        digest,name=line.split('  ',1);assert hashlib.sha256(files[name]).hexdigest()==digest
    return len(parsed)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'exports/ResearchKit-学生上手包-合并指南-20261120.zip')
    args=parser.parse_args()
    files=outputs();html_count=validate(files)
    package=args.output;checksum=package.with_name(package.name+'.sha256')
    if args.check:
        with ZipFile(package) as archive:
            assert set(archive.namelist())=={TOP+'/'+name for name in files}
            for name,data in files.items():assert archive.read(TOP+'/'+name)==data,name
        assert checksum.read_text()==hashlib.sha256(package.read_bytes()).hexdigest()+'  '+package.name+'\n'
    else:
        package.parent.mkdir(parents=True,exist_ok=True)
        if package.exists():
            with ZipFile(package) as archive:
                assert set(archive.namelist())=={TOP+'/'+name for name in files} and all(archive.read(TOP+'/'+name)==data for name,data in files.items()),'Existing kit differs; choose a new output path instead of overwriting.'
        else:
            temporary=package.with_name('.'+package.name+'.new')
            try:
                with ZipFile(temporary,'w',ZIP_DEFLATED) as archive:
                    for name,data in sorted(files.items()):
                        info=ZipInfo(TOP+'/'+name,(2026,11,20,0,0,0));info.compress_type=ZIP_DEFLATED
                        info.external_attr=0o100644<<16;archive.writestr(info,data)
                temporary.replace(package)
            finally:
                temporary.unlink(missing_ok=True)
        checksum.write_text(hashlib.sha256(package.read_bytes()).hexdigest()+'  '+package.name+'\n')
    print(json.dumps({'package':str(package),'files':len(files),'offline_html_pages':html_count,
        'links_checked':True,'launchers_unchanged':True,'bytes':package.stat().st_size,'check':args.check},ensure_ascii=False))

if __name__=='__main__':main()
