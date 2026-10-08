# -*- coding: utf-8 -*-
"""Handlers for application signals."""
from .models.terminal import clean_order
from .storage.strategies import SoftDeleteStrategy


def clean_data(sender, command, email):
    """Clean data for account with email."""
    with command.storage:
        _clean_data(command.storage)


def _clean_data(storage):
    for model in clean_order:
        instances = storage.get_all(model)
        for i in instances:
            storage.delete(i)

    deleted_set = SoftDeleteStrategy(storage).get_delete_sets()
    storage.confirm_delete(deleted_set)
