#!/usr/bin/env python3

# https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack

import contextlib
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePath
from typing import Any, BinaryIO, Literal, NotRequired
from typing_extensions import TypedDict

import click
import typeguard

type ModrinthIndexEnvironmentStatus = (
    Literal["required"] | Literal["optional"] | Literal["unsupported"]
)


class ModrinthIndexFile(TypedDict, total=True, extra_items=Any):
    path: str
    hashes: dict[str, str]
    env: NotRequired[dict[str, ModrinthIndexEnvironmentStatus]]
    downloads: list[str]
    fileSize: int


class ModrinthIndex(TypedDict, total=True, extra_items=Any):
    formatVersion: Literal[1]
    game: str
    versionId: str
    name: str
    files: list[ModrinthIndexFile]
    dependencies: dict[str, str]


MODRINTH_INDEX_FILENAME = "modrinth.index.json"
ALLOWED_MC_SUBDIRS = frozenset({"mods", "datapacks", "resourcepacks", "shaderpacks"})
MAX_DOWNLOAD_TRIES = 5


@typeguard.typechecked
def load_modrinth_index(*, _decoder=json.JSONDecoder(strict=False)) -> ModrinthIndex:
    with open(MODRINTH_INDEX_FILENAME, "r", encoding="utf-8") as f:
        return _decoder.decode(f.read())


@typeguard.typechecked
def check_modrinth_file_env_unsupported(file_data: ModrinthIndexFile, env: str) -> bool:
    if env == "server" and file_data["path"].casefold().startswith("shaderpacks/"):
        # shaderpacks are client-only.
        return True

    # If the file does not specify any env, assume that it is supported
    # on both the client and server. This helps prevents false negatives.
    return "env" in file_data and file_data["env"].get(env) == "unsupported"


@typeguard.typechecked
def download_modrinth_file(
    file_data: ModrinthIndexFile,
    mc_dir: Path,
    *,
    user_agent: str | None,
    may_replace: bool,
    allow_hash_mismatch: bool,
) -> None:
    file_path = PurePath(file_data["path"])
    # Make sure that the path given is safe and
    # sensible, to prevent unexpected behavior.
    if (
        len(file_path.parts) != 2
        or ".." in file_path.parts
        or file_path.parent.name not in ALLOWED_MC_SUBDIRS
    ):
        raise ValueError(f"Invalid file path: {file_path}")

    real_file_path = mc_dir / file_path
    if not may_replace and real_file_path.exists():
        raise FileExistsError(f"File already exists: {real_file_path}")

    real_file_path.parent.mkdir(exist_ok=True)

    urls = set(file_data["downloads"])
    if not urls:
        raise ValueError("No URLs provided")

    # A SHA-512 hash is required by the Modrinth standard,
    # so we can expect the modpack to provide one for every file.
    checksum = file_data["hashes"].get("sha512")
    if not checksum:
        raise ValueError("No SHA-512 hash provided")
    checksum = checksum.casefold()

    tries_left = MAX_DOWNLOAD_TRIES

    while True:
        tries_left -= 1
        try:
            url = urls.pop()
            if not url.startswith("https://"):
                raise ValueError(f"Invalid URL: {url}")

            click.echo(f"\tTrying URL: {url}")

            req = urllib.request.Request(url)
            if user_agent is not None:
                req.add_header("User-Agent", user_agent)

            with urllib.request.urlopen(req) as f:
                data = f.read()
                if hashlib.sha512(data).hexdigest().casefold() != checksum:
                    if not allow_hash_mismatch:
                        raise ValueError("Hash verification failed")
                    click.secho("\tHash verification FAILED", err=True, fg="yellow")

                # Successful download.
                real_file_path.write_bytes(data)
                return
        except urllib.request.HTTPError as e:
            click.echo(f"\tDownload failed: {e}", err=True)
            if not urls or tries_left < 1:
                raise


@typeguard.typechecked
def install_overrides_if_exist(override_dir: Path, mc_dir: Path) -> None:
    if not override_dir.exists():
        return

    click.echo(f"Copying overrides from directory: {override_dir}")
    shutil.copytree(override_dir, mc_dir, symlinks=True, dirs_exist_ok=True)


@click.group()
def mc_modpack():
    """
    Tool for handling Minecraft modpack files in the Modrinth format (.mrpack).
    """


@mc_modpack.command(short_help="display modpack information")
@click.argument("files", type=click.File("rb"), nargs=-1, required=True)
@click.option("-d", "--dump-index", is_flag=True, help="Dump formatted index JSON.")
@click.option("-l", "--list-files", is_flag=True, help="List all files in modpack.")
@click.option(
    "-e",
    "--env",
    type=click.Choice(("client", "server"), case_sensitive=False),
    help="With -l, only list files that support a specific environment.",
)
def query(files: tuple[BinaryIO, ...], **kwargs):
    """Extracts and prints information from modpack files."""
    for file in files:
        with zipfile.ZipFile(file, "r") as zf:
            data = json.loads(zf.read(MODRINTH_INDEX_FILENAME).decode("utf-8"))

            if kwargs["dump_index"]:
                print(json.dumps(data, ensure_ascii=True, indent=True))
                continue

            if kwargs["list_files"]:
                for file_data in sorted(
                    data["files"], key=lambda file_data: file_data["path"]
                ):
                    if kwargs[
                        "env"
                    ] is not None and check_modrinth_file_env_unsupported(
                        file_data, kwargs["env"]
                    ):
                        continue

                    click.echo(file_data["path"])

                continue

            click.echo(f"""\
Modpack name\t\t: {data["name"]}
Modpack version\t\t: {data["versionId"]}
Minecraft version\t: {data["dependencies"].get("minecraft", "(unspecified)")}
No. of files listed\t: {len(data["files"])}
Download size (bytes)\t: {sum(file_data["fileSize"] for file_data in data["files"])}
""")


@mc_modpack.command(short_help="download files and install modpack")
@click.argument("file", type=click.File("rb"), required=True)
@click.option(
    "-m",
    "--mc-dir",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, path_type=Path),
    default=Path(".minecraft"),
    help="Specify .minecraft directory for installation. Will be created as necessary."
    " Defaults to .minecraft in the current working directory.",
)
@click.option(
    "-e",
    "--env",
    type=click.Choice(("client", "server"), case_sensitive=False),
    help="Only download and install files that support a specific environment.",
)
@click.option(
    "-y",
    "--confirmed",
    is_flag=True,
    help="Do not prompt for confirmation before downloading files.",
)
@click.option(
    "--user-agent", metavar="STRING", help="Specify HTTP User-Agent for downloads."
)
@click.option(
    "-j",
    "--existing",
    type=click.Choice(("skip", "replace", "fail"), case_sensitive=False),
    default="skip",
    help="Specify how to handle existing files.",
)
@click.option(
    "--ignore-mismatched-hashes",
    is_flag=True,
    help="Do not stop when hash verification fails.",
)
@click.option("-n", "--no-overrides", is_flag=True, help="Do not install overrides.")
def install(file: BinaryIO, **kwargs):
    """
    Downloads files in a modpack and installs the modpack into a
    .minecraft directory, including any overrides.
    """

    # Get the absolute path of the Minecraft directory;
    # this must be done before going into the temporary directory.
    kwargs["mc_dir"] = kwargs["mc_dir"].resolve()

    with (
        tempfile.TemporaryDirectory() as dirname,
        contextlib.chdir(dirname),
        zipfile.ZipFile(file, "r") as zf,
    ):
        zf.extractall()
        data = load_modrinth_index()

        click.echo(
            f"{data["name"]} {data["versionId"]}"
            f" for Minecraft {data["dependencies"].get("minecraft", "(unspecified version)")}"
            f" will be installed to: {kwargs["mc_dir"]}"
        )

        if not (
            kwargs["confirmed"]
            or click.confirm(
                f"Proceed with download of {len(data["files"])} file(s)?", default=True
            )
        ):
            raise click.ClickException("Operation canceled by user")

        kwargs["mc_dir"].mkdir(exist_ok=True)

        for i, file_data in enumerate(data["files"]):
            click.echo(f"File ({i+1}/{len(data["files"])}): {file_data["path"]}")

            if kwargs["env"] is not None and check_modrinth_file_env_unsupported(
                file_data, kwargs["env"]
            ):
                click.echo(f"\tSkipped (does not support environment: {kwargs["env"]})")

            try:
                download_modrinth_file(
                    file_data,
                    kwargs["mc_dir"],
                    user_agent=kwargs["user_agent"],
                    may_replace=kwargs["existing"] == "replace",
                    allow_hash_mismatch=kwargs["ignore_mismatched_hashes"],
                )
            except (ValueError, FileExistsError, urllib.request.HTTPError) as e:
                if isinstance(e, FileExistsError) and kwargs["existing"] == "skip":
                    click.echo("\tSkipped (file already exists)")
                    continue

                raise click.ClickException(f"Unable to download file: {e}")

        if not kwargs["no_overrides"]:
            install_overrides_if_exist(Path("overrides"), kwargs["mc_dir"])

            # Environment-specific overrides should be applied only
            # after the generic overrides (if any) have been installed.
            if kwargs["env"] == "client":
                install_overrides_if_exist(Path("client-overrides"), kwargs["mc_dir"])
            elif kwargs["env"] == "server":
                install_overrides_if_exist(Path("server-overrides"), kwargs["mc_dir"])


if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    mc_modpack()
