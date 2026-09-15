from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedBlock:
    content: str
    page_no: int | None = None
    is_table: bool = False


@dataclass
class ParseResult:
    blocks: list[ParsedBlock] = field(default_factory=list)
    page_count: int = 0


class Parser(ABC):
    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        ...


REGISTRY: dict[str, type[Parser]] = {}


def register(ext: str):
    def deco(cls: type[Parser]) -> type[Parser]:
        REGISTRY[ext] = cls
        return cls

    return deco


def get_parser(ext: str) -> Parser:
    return REGISTRY[ext.lower()]()
