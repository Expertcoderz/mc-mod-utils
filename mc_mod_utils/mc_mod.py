#!/usr/bin/env python3

# https://wiki.fabricmc.net/documentation:fabric_mod_json
# https://docs.modrinth.com/api/

import json
import zipfile
from pathlib import Path
from typing import Any, Iterable, Literal, NotRequired
from typing_extensions import TypedDict

import click
import typeguard


class FabricModfileData(TypedDict, total=True, extra_items=Any):
    schemaVersion: Literal[1]
    id: str
    name: NotRequired[str]
    version: str
    environment: NotRequired[Literal["*", "client", "server"]]


FABRIC_MOD_JSON_PATH = "fabric.mod.json"


def extract_modfile_data(
    modfile_path: Path, _decoder=json.JSONDecoder(strict=False)
) -> FabricModfileData:
    typeguard.check_argument_types()

    with zipfile.ZipFile(modfile_path, "r") as f:
        try:
            data = _decoder.decode(f.read(FABRIC_MOD_JSON_PATH).decode())
        except json.JSONDecodeError as e:
            raise click.ClickException(
                f"Failed to parse mod metadata for {modfile_path}: {e}"
            )

    try:
        return typeguard.check_return_type(data)
    except typeguard.TypeCheckError as e:
        raise click.ClickException(
            f"Failed to validate mod metadata for {modfile_path}: {e}"
        )


def iterate_modfiles(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from map(
                lambda subpath: (subpath, extract_modfile_data(subpath)),
                path.glob("*.jar", case_sensitive=False),
            )
        else:
            yield path, extract_modfile_data(path)


@click.group()
def mc_mod():
    """
    Tool for handling Minecraft mod files (.jar).

    Currently, only Fabric mods are supported.
    """


@mc_mod.command(short_help="display mod information")
@click.argument(
    "files",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    nargs=-1,
    required=True,
)
@click.option("-d", "--dump", is_flag=True, help="Dump formatted JSON.")
def query(files: tuple[Path, ...], **kwargs):
    """
    Extracts and prints metadata from mod files.

    If a directory is supplied, all *.jar files immediately under
    the directory will be considered.
    """

    for path, data in iterate_modfiles(files):
        del path

        if kwargs["dump"]:
            click.echo(json.dumps(data, indent=True))
            continue

        click.echo(f"""\
ID\t: {data["id"]}
Name\t: {data.get("name", data["id"])}
Version\t: {data["version"]}
License\t: {data["license"]}
""")


@mc_mod.command(short_help="deduplicate mods")
@click.argument(
    "files",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    nargs=-1,
    required=True,
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Report duplicates but do not perform removal.",
)
@click.option(
    "-a",
    "--archive",
    metavar="EXT",
    help="Append EXT to the mod's filename instead of removing it.",
)
def cleanup(files: tuple[Path, ...], **kwargs):
    """
    Checks the given mod files for mods with identical IDs, and
    removes all but the one with the latest file modification time.

    If a directory is supplied, all *.jar files immediately under
    the directory will be considered.
    """

    mod_mtimes: dict[str, tuple[int, Path]] = {}

    for path, data in iterate_modfiles(files):
        mtime = path.stat().st_mtime
        other_mtime, other_path = mod_mtimes.get(data["id"], (None, None))

        if other_mtime is None:
            mod_mtimes[data["id"]] = (mtime, path)
            continue

        if mtime == other_mtime:
            click.secho(f"Same mtimes:\t{other_path}\n\t\t{path}", fg="yellow")
            continue

        if mtime < other_mtime:
            target_path = path
        else:
            target_path = other_path
            mod_mtimes[data["id"]] = (mtime, path)

        click.echo(f"Outdated mod: {target_path}\n\tSuperseded by: {
            other_path if mtime < other_mtime else path}")

        if kwargs["dry_run"]:
            continue

        if kwargs["archive"]:
            target_path.rename(target_path.name + kwargs["archive"])
        else:
            target_path.unlink()


if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    mc_mod()
