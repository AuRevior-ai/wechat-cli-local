import unittest

from wechat_cli.outbound.models import OutboundError, TargetResolutionError
from wechat_cli.outbound.resolver import resolve_target_from_contacts


class OutboundResolverTests(unittest.TestCase):
    def setUp(self):
        self.contacts = [
            {"username": "wxid_alice", "nick_name": "Alice", "remark": "客户A"},
            {"username": "wxid_bob", "nick_name": "Bob", "remark": ""},
            {"username": "room1@chatroom", "nick_name": "产品群", "remark": ""},
        ]

    def test_stable_username_wins(self):
        target = resolve_target_from_contacts("wxid_alice", self.contacts)
        self.assertEqual("wxid_alice", target.username)
        self.assertEqual("private", target.kind)

    def test_exact_remark_resolves(self):
        target = resolve_target_from_contacts("客户A", self.contacts)
        self.assertEqual("wxid_alice", target.username)
        self.assertEqual("客户A", target.display_name)

    def test_group_kind_is_preserved(self):
        target = resolve_target_from_contacts("产品群", self.contacts)
        self.assertEqual("room1@chatroom", target.username)
        self.assertEqual("group", target.kind)

    def test_ambiguous_exact_name_is_blocked(self):
        contacts = self.contacts + [
            {"username": "wxid_other", "nick_name": "Alice", "remark": "客户A"},
        ]
        with self.assertRaises(TargetResolutionError) as cm:
            resolve_target_from_contacts("客户A", contacts)
        self.assertEqual(OutboundError.TARGET_AMBIGUOUS, cm.exception.code)

    def test_not_found_is_blocked(self):
        with self.assertRaises(TargetResolutionError) as cm:
            resolve_target_from_contacts("不存在", self.contacts)
        self.assertEqual(OutboundError.TARGET_NOT_FOUND, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
