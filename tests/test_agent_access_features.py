import unittest
from tests._path import *
from agent_foundation.access import Grant, LocalAccess, Principal
from agent_foundation.features import Feature, FeatureDisabled, Features


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.alice = Principal('org', 'alice')
        self.bob = Principal('org', 'bob')
        self.group = Principal('org', 'group:sales')
        self.read = Grant(self.alice, 'read', 'task', 'task-1')
        self.access = LocalAccess({'alice-token': self.alice, 'bob-token': self.bob}, {self.read})

    def test_authentication_and_revocation(self):
        self.assertEqual(self.alice, self.access.authenticate('alice-token'))
        with self.assertRaises(PermissionError): self.access.authenticate('fake')
        self.access.require(self.alice, 'read', 'task', 'task-1')
        with self.assertRaises(PermissionError): self.access.require(self.bob, 'read', 'task', 'task-1')
        self.access.replace_grants(set())
        with self.assertRaises(PermissionError): self.access.require(self.alice, 'read', 'task', 'task-1')

    def test_group_requires_group_and_resource_grants(self):
        with self.assertRaises(PermissionError):
            self.access.require_group_delivery(self.alice, 'sales', 'task', 'task-1')
        self.access.replace_grants({self.read, Grant(self.alice, 'deliver', 'group', 'sales'),
                                    Grant(self.group, 'read', 'task', 'task-1')})
        self.access.require_group_delivery(self.alice, 'sales', 'task', 'task-1')
        with self.assertRaises(ValueError): Grant(self.alice, 'read', 'task', '*')


class FeatureTests(unittest.TestCase):
    def test_disabled_dependencies_and_versions(self):
        modules = [Feature('core', '1'), Feature('knowledge', '1', ('core',))]
        with self.assertRaises(FeatureDisabled): Features(modules, {'knowledge'}).require('knowledge')
        self.assertEqual('1', Features(modules, {'core', 'knowledge'}).require('knowledge').version)
        with self.assertRaises(FeatureDisabled): Features(modules, set()).require('unknown')

    def test_circular_dependencies_fail_closed(self):
        features = Features([Feature('a', '1', ('b',)), Feature('b', '1', ('a',))], {'a', 'b'})
        with self.assertRaises(FeatureDisabled): features.require('a')
