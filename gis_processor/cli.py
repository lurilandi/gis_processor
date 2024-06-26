from logging import ERROR
from logging import INFO
from logging import Logger
from os import PathLike
from pathlib import Path
from shutil import copy
from sqlite3 import connect
from sys import stdout
from traceback import format_tb
from uuid import UUID
from uuid import uuid4

from acacore.__version__ import __version__ as __acacore_version__
from acacore.database import FileDB
from acacore.models.file import File
from acacore.models.history import HistoryEntry
from acacore.models.reference_files import IgnoreAction
from acacore.utils.helpers import ExceptionManager
from acacore.utils.log import setup_logger
from click import argument
from click import command
from click import Context
from click import option
from click import pass_context
from click import Path as ClickPath
from click import version_option

from .__version__ import __version__
from .processor import find_processor
from .processor import Processor


def handle_start(ctx: Context, database: FileDB, *loggers: Logger):
    program_start: HistoryEntry = HistoryEntry.command_history(
        ctx,
        "start",
        data={"acacore": __acacore_version__},
        add_params_to_data=True,
    )

    database.history.insert(program_start)

    program_start.log(INFO, *loggers)


def handle_end(ctx: Context, database: FileDB, exception: ExceptionManager, *loggers: Logger, commit: bool = True):
    program_end: HistoryEntry = HistoryEntry.command_history(
        ctx,
        "end",
        data=repr(exception.exception) if exception.exception else None,
        reason="".join(format_tb(exception.traceback)) if exception.traceback else None,
    )

    program_end.log(ERROR if exception.exception else INFO, *loggers)

    if database.is_open:
        database.history.insert(program_end)
        if commit:
            database.commit()


def file_not_found_error(
    ctx: Context,
    file_type: str,
    path: str | PathLike,
    uuid: UUID | None,
) -> HistoryEntry:
    return HistoryEntry.command_history(ctx, f"file.{file_type}:error", uuid, None, f"{path} not in database")


def get_file(db: FileDB, path: str | PathLike) -> File | None:
    return db.files.select(
        where="relative_path = ?",
        parameters=[str(Path(path))],
        limit=1,
    ).fetchone()


@command("gis-processor")
@argument("root", nargs=1, type=ClickPath(exists=True, file_okay=False, writable=True, resolve_path=True))
@argument("avid", nargs=1, type=ClickPath(exists=True, dir_okay=False, readable=True, resolve_path=True))
@option("--dry-run", is_flag=True, default=False, help="Show changes without committing them.")
@version_option(__version__)
@pass_context
def app(ctx: Context, root: str | PathLike, avid: str | PathLike, dry_run: bool):
    root, avid = Path(root), Path(avid)
    database_path: Path = root / "_metadata" / "files.db"

    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    if not avid.is_file():
        raise FileNotFoundError(avid)

    program_name: str = ctx.find_root().command.name
    logger: Logger = setup_logger(program_name, files=[database_path.parent / f"{program_name}.log"], streams=[stdout])

    with connect(avid) as avid_conn:
        if not (processor_cls := find_processor(avid_conn)):
            raise ValueError(f"{avid!r} is not recognised")
        processor: Processor = processor_cls(avid_conn)

        with FileDB(database_path) as db:
            handle_start(ctx, db, logger)

            with ExceptionManager() as exception:
                for main_file_orig in processor.find_main_files():
                    main_file: File | None = get_file(db, p := processor.file_to_path(main_file_orig))

                    if not main_file:
                        file_not_found_error(ctx, "main", p, None).log(ERROR, logger)
                        continue
                    elif (p := main_file.get_absolute_path(root)).exists():
                        file_not_found_error(ctx, "main", p, main_file.uuid).log(ERROR, logger)
                        continue

                    aux_files: list[tuple[File, File]] = []

                    for aux_file_orig in processor.find_auxiliary_files(main_file_orig):
                        aux_file: File | None = get_file(db, p := processor.file_to_path(aux_file_orig))

                        if not aux_file:
                            file_not_found_error(ctx, "aux", p, None).log(ERROR, logger)
                            aux_files = []
                            break
                        elif not (p := aux_file.get_absolute_path(root)).exists():
                            file_not_found_error(ctx, "aux", p, aux_file.uuid).log(ERROR, logger)
                            aux_files = []
                            break

                        if aux_file.action == "ignore":
                            continue

                        aux_file.action = "ignore"
                        aux_file.action_data = IgnoreAction(reason="Auxiliary file")
                        new_path: Path = main_file.relative_path.with_name(aux_file.name)

                        aux_file_copy: File

                        if aux_file_copy_ := get_file(db, new_path):
                            if aux_file_copy_.checksum != aux_file.checksum:
                                HistoryEntry.command_history(
                                    ctx, "file.aux:error", reason=f"{p} already exists with different hash"
                                ).log(ERROR, logger)
                                aux_files = []
                                break
                            aux_file_copy = aux_file_copy_
                        else:
                            aux_file_copy = aux_file.model_copy(update={"uuid": uuid4(), "relative_path": new_path})
                            if (p := aux_file_copy.get_absolute_path(root)).exists():
                                HistoryEntry.command_history(ctx, "file.aux:error", reason=f"{p} already exists").log(
                                    ERROR, logger
                                )
                                aux_files = []
                                break

                        aux_files.append((aux_file, aux_file_copy))

                    for aux_file, aux_file_copy in aux_files:
                        event = HistoryEntry.command_history(
                            ctx,
                            "file.aux:copy",
                            aux_file_copy.uuid,
                            [aux_file.relative_path, aux_file_copy.relative_path],
                        )

                        event.log(INFO, logger)

                        if dry_run:
                            continue

                        try:
                            copy(aux_file.get_absolute_path(root), aux_file_copy.get_absolute_path(root))
                            db.files.insert(aux_file_copy, replace=True)
                            db.files.update(aux_file)
                            db.history.insert(event)
                        except BaseException:
                            aux_file_copy.get_absolute_path(root).unlink(missing_ok=True)
                            raise

            handle_end(ctx, db, exception, logger, commit=not dry_run)
