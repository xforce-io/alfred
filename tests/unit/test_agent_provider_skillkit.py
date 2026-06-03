"""TDD A4: 把 skillkit 注册收进 provider。

telegram_channel 裸访问 agent.global_skills.installedSkillset.hasSkill/addSkillkit
(dolphin 特定)。收成 provider.has_skill / register_skillkit;DolphinProvider 委托
dolphin installedSkillset,MilkieProvider 将占位(Python skill 桥待 milkie#87)。
"""
from src.everbot.core.agent.provider.dolphin.provider import DolphinProvider


class FakeInstalled:
    def __init__(self):
        self.skills = set()
        self.added = []

    def hasSkill(self, n):
        return n in self.skills

    def addSkillkit(self, k):
        self.added.append(k)


class FakeGS:
    def __init__(self):
        self.installedSkillset = FakeInstalled()


class FakeAgent:
    def __init__(self):
        self.global_skills = FakeGS()


def test_has_skill_true():
    a = FakeAgent()
    a.global_skills.installedSkillset.skills.add("_tg_send_file")
    assert DolphinProvider().has_skill(a, "_tg_send_file") is True


def test_has_skill_false():
    assert DolphinProvider().has_skill(FakeAgent(), "_tg_send_file") is False


def test_register_skillkit_adds():
    a = FakeAgent()
    kit = object()
    DolphinProvider().register_skillkit(a, kit)
    assert a.global_skills.installedSkillset.added == [kit]


def test_no_global_skills_tolerated():
    class Bare:
        global_skills = None

    assert DolphinProvider().has_skill(Bare(), "x") is False
    DolphinProvider().register_skillkit(Bare(), object())  # 不应抛错
