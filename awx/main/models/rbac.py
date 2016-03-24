# Copyright (c) 2016 Ansible, Inc.
# All Rights Reserved.

# Python
import logging
import threading
import contextlib

# Django
from django.db import models, transaction
from django.db.models import Q
from django.db.models.aggregates import Max
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey

# AWX
from django.contrib.auth.models import User # noqa
from awx.main.models.base import * # noqa

__all__ = [
    'Role',
    'RolePermission',
    'batch_role_ancestor_rebuilding',
    'get_user_permissions_on_resource',
    'get_role_permissions_on_resource',
    'ROLE_SINGLETON_SYSTEM_ADMINISTRATOR',
    'ROLE_SINGLETON_SYSTEM_AUDITOR',
]

logger = logging.getLogger('awx.main.models.rbac')

ROLE_SINGLETON_SYSTEM_ADMINISTRATOR='System Administrator'
ROLE_SINGLETON_SYSTEM_AUDITOR='System Auditor'

ALL_PERMISSIONS = {'create': True, 'read': True, 'update': True, 'delete': True,
                   'write': True, 'scm_update': True, 'use': True, 'execute': True}


tls = threading.local() # thread local storage

@contextlib.contextmanager
def batch_role_ancestor_rebuilding(allow_nesting=False):
    '''
    Batches the role ancestor rebuild work necessary whenever role-role
    relations change. This can result in a big speedup when performing
    any bulk manipulation.

    WARNING: Calls to anything related to checking access/permissions
    while within the context of the batch_role_ancestor_rebuilding will
    likely not work.
    '''

    batch_role_rebuilding = getattr(tls, 'batch_role_rebuilding', False)

    try:
        setattr(tls, 'batch_role_rebuilding', True)
        if not batch_role_rebuilding:
            setattr(tls, 'roles_needing_rebuilding', set())
        yield

    finally:
        setattr(tls, 'batch_role_rebuilding', batch_role_rebuilding)
        if not batch_role_rebuilding:
            rebuild_set = getattr(tls, 'roles_needing_rebuilding')
            with transaction.atomic():
                for role in Role.objects.filter(id__in=list(rebuild_set)).all():
                    # TODO: We can reduce this to one rebuild call with our new upcoming rebuild method.. do this
                    role.rebuild_role_ancestor_list()
            delattr(tls, 'roles_needing_rebuilding')


class Role(CommonModelNameNotUnique):
    '''
    Role model
    '''

    class Meta:
        app_label = 'main'
        verbose_name_plural = _('roles')
        db_table = 'main_rbac_roles'

    singleton_name = models.TextField(null=True, default=None, db_index=True, unique=True)
    parents = models.ManyToManyField('Role', related_name='children')
    ancestors = models.ManyToManyField('Role', related_name='descendents') # auto-generated by `rebuild_role_ancestor_list`
    members = models.ManyToManyField('auth.User', related_name='roles')
    content_type = models.ForeignKey(ContentType, null=True, default=None)
    object_id = models.PositiveIntegerField(null=True, default=None)
    content_object = GenericForeignKey('content_type', 'object_id')

    def save(self, *args, **kwargs):
        super(Role, self).save(*args, **kwargs)
        self.rebuild_role_ancestor_list()

    def get_absolute_url(self):
        return reverse('api:role_detail', args=(self.pk,))


    def rebuild_role_ancestor_list(self):
        '''
        Updates our `ancestors` map to accurately reflect all of the ancestors for a role

        You should never need to call this. Signal handlers should be calling
        this method when the role hierachy changes automatically.

        Note that this method relies on any parents' ancestor list being correct.
        '''
        global tls
        batch_role_rebuilding = getattr(tls, 'batch_role_rebuilding', False)

        if batch_role_rebuilding:
            roles_needing_rebuilding = getattr(tls, 'roles_needing_rebuilding')
            roles_needing_rebuilding.add(self.id)
            return

        actual_ancestors = set(Role.objects.filter(id=self.id).values_list('parents__ancestors__id', flat=True))
        actual_ancestors.add(self.id)
        if None in actual_ancestors:
            actual_ancestors.remove(None)
        stored_ancestors = set(self.ancestors.all().values_list('id', flat=True))

        # If it differs, update, and then update all of our children
        if actual_ancestors != stored_ancestors:
            for id in actual_ancestors - stored_ancestors:
                self.ancestors.add(id)
            for id in stored_ancestors - actual_ancestors:
                self.ancestors.remove(id)

            for child in self.children.all():
                child.rebuild_role_ancestor_list()

    @staticmethod
    def visible_roles(user):
        return Role.objects.filter(Q(descendents__in=user.roles.filter()) | Q(ancestors__in=user.roles.filter()))

    @staticmethod
    def singleton(name):
        try:
            return Role.objects.get(singleton_name=name)
        except Role.DoesNotExist:
            ret = Role.objects.create(singleton_name=name, name=name)
            return ret

    def is_ancestor_of(self, role):
        return role.ancestors.filter(id=self.id).exists()


class RolePermission(CreatedModifiedModel):
    '''
    Defines the permissions a role has
    '''

    class Meta:
        app_label = 'main'
        verbose_name_plural = _('permissions')
        db_table = 'main_rbac_permissions'
        index_together = [
            ('content_type', 'object_id')
        ]

    role = models.ForeignKey(
        Role,
        null=False,
        on_delete=models.CASCADE,
        related_name='permissions',
    )
    content_type = models.ForeignKey(ContentType, null=False, default=None)
    object_id = models.PositiveIntegerField(null=False, default=None)
    resource = GenericForeignKey('content_type', 'object_id')
    auto_generated = models.BooleanField(default=False)

    create     = models.IntegerField(default = 0)
    read       = models.IntegerField(default = 0)
    write      = models.IntegerField(default = 0)
    delete     = models.IntegerField(default = 0)
    update     = models.IntegerField(default = 0)
    execute    = models.IntegerField(default = 0)
    scm_update = models.IntegerField(default = 0)
    use        = models.IntegerField(default = 0)



def get_user_permissions_on_resource(resource, user):
    '''
    Returns a dict (or None) of the permissions a user has for a given
    resource.

    Note: Each field in the dict is the `or` of all respective permissions
    that have been granted to the roles that are applicable for the given
    user.

    In example, if a user has been granted read access through a permission
    on one role and write access through a permission on a separate role,
    the returned dict will denote that the user has both read and write
    access.
    '''

    if type(user) == User:
        roles = user.roles.all()
    else:
        accessor_type = ContentType.objects.get_for_model(user)
        roles = Role.objects.filter(content_type__pk=accessor_type.id,
                                    object_id=user.id)

    qs = RolePermission.objects.filter(
        content_type=ContentType.objects.get_for_model(resource),
        object_id=resource.id,
        role__ancestors__in=roles,
    )

    res = qs = qs.aggregate(
        create = Max('create'),
        read = Max('read'),
        write = Max('write'),
        update = Max('update'),
        delete = Max('delete'),
        scm_update = Max('scm_update'),
        execute = Max('execute'),
        use = Max('use')
    )
    if res['read'] is None:
        return None
    return res

def get_role_permissions_on_resource(resource, role):
    '''
    Returns a dict (or None) of the permissions a role has for a given
    resource.

    Note: Each field in the dict is the `or` of all respective permissions
    that have been granted to either the role or any descendents of that role.
    '''

    qs = RolePermission.objects.filter(
        content_type=ContentType.objects.get_for_model(resource),
        object_id=resource.id,
        role__ancestors=role
    )

    res = qs = qs.aggregate(
        create = Max('create'),
        read = Max('read'),
        write = Max('write'),
        update = Max('update'),
        delete = Max('delete'),
        scm_update = Max('scm_update'),
        execute = Max('execute'),
        use = Max('use')
    )
    if res['read'] is None:
        return None
    return res
