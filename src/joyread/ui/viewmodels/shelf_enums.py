"""Small shelf presentation enums shared by chrome without loading Library state."""

from enum import StrEnum


class ViewMode(StrEnum):
    GRID = "grid"
    LIST = "list"


class SortField(StrEnum):
    CUSTOM = "Custom"
    ADD_TIME = "Add Time"
    TITLE = "Title"
    AUTHOR = "Author"
