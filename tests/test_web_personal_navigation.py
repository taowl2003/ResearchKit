from html.parser import HTMLParser
from pathlib import Path


class Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.ids = set()
        self.section_parents = {}
        self.navs = {'personal': [], 'teacher': [], 'shared': []}
        self.div_groups = []
        self.group = None
        self.button = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identity = attrs.get('id')
        if identity:
            assert identity not in self.ids, identity
            self.ids.add(identity)
        if tag == 'section':
            self.section_parents[identity] = self.stack[-1] if self.stack else None
            self.stack.append(identity)
        if tag == 'div':
            self.div_groups.append(self.group)
            classes = set(attrs.get('class', '').split())
            for class_name, group in [('nav-personal', 'personal'), ('nav-teacher', 'teacher'),
                                      ('nav-feedback', 'shared')]:
                if class_name in classes:
                    self.group = group
        if tag == 'button' and self.group and attrs.get('data-page'):
            self.button = self.group
        if tag == 'main' and self.group:
            self.group = None

    def handle_endtag(self, tag):
        if tag == 'div':
            self.group = self.div_groups.pop()
        if tag == 'section':
            assert self.stack, 'unbalanced section'
            self.stack.pop()
        if tag == 'button':
            self.button = None
        if tag == 'aside':
            self.group = None

    def handle_data(self, text):
        if self.button and text.strip():
            self.navs[self.button].append(text.strip())


def test_personal_and_teacher_navigation_are_separate_and_task_section_belongs_to_projects():
    source = (Path(__file__).resolve().parents[1] / 'core/researchkit/web_static/index.html').read_text()
    shell = Shell()
    shell.feed(source)
    assert not shell.stack
    assert shell.navs['personal'] == ['项目', '固定提交', '数据资源', '算法库']
    assert shell.navs['teacher'] == ['案例管理', '算法管理', '数据管理', '审核入库', '分发任务', '资源分发', '资源管理']
    assert shell.navs['shared'] == ['消息反馈']
    assert shell.section_parents['distributions'] == 'projects'
    assert shell.section_parents['resource-management'] is None
    assert shell.section_parents['composer'] is None
    assert 'jobs' not in shell.section_parents
    assert shell.section_parents['feedback'] is None
    assert shell.section_parents['distributed-tasks'] is None
    assert all(name in shell.ids for name in ['data-management', 'retired-bin', 'cleanup-bin'])
