#!/usr/bin/env python3
"""Build safe static help and synchronized Windows documentation, not launchers.

Development dependency: mistune. No application/runtime dependency is added.
"""
from pathlib import Path
import argparse
import importlib.util
import hashlib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT=Path(__file__).resolve().parents[1]
def help_html():
    kit=student_kit()
    return kit.document_html('ResearchKit 学生使用指南',(ROOT/'docs/学生使用指南.md').read_text())

def student_kit():
    spec=importlib.util.spec_from_file_location('student_kit_builder',ROOT/'tools/build-student-kit.py')
    kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
    return kit

def windows_files():
    kit=student_kit()
    files=kit.outputs()
    kit.validate(files)
    return files

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    target=ROOT/'core/researchkit/web_static/help.html';html=help_html()
    files=windows_files();package=ROOT/'exports/ResearchKit-Windows.zip';checksum=package.with_suffix('.zip.sha256')
    if args.check:
        assert target.read_text()==html,'Embedded help is stale'
        with ZipFile(package) as archive:
            assert set(archive.namelist())=={'ResearchKit-Windows/'+name for name in files}
            for name,data in files.items():assert archive.read('ResearchKit-Windows/'+name)==data,name
        assert checksum.read_text()==hashlib.sha256(package.read_bytes()).hexdigest()+'  '+package.name+'\n'
    else:
        target.write_text(html)
        with ZipFile(package,'w',ZIP_DEFLATED) as archive:
            for name,data in sorted(files.items()):
                info=ZipInfo('ResearchKit-Windows/'+name,(2026,11,20,0,0,0));info.compress_type=ZIP_DEFLATED
                archive.writestr(info,data)
        checksum.write_text(hashlib.sha256(package.read_bytes()).hexdigest()+'  '+package.name+'\n')

if __name__=='__main__':main()
