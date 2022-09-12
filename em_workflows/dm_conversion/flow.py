import json
from pathlib import Path
from typing import List
from prefect import Flow, task, Parameter, unmapped
from prefect.run_configs import LocalRun
from em_workflows.shell_task_echo import ShellTaskEcho
from prefect.engine import signals
from pytools.meta import is_int16
from pytools.convert import file_to_uint8

from em_workflows.config import Config
from em_workflows.utils import utils

shell_task = ShellTaskEcho(log_stderr=True, return_all=True, stream_output=True)


@task
def convert_if_int16_tiff(fp: Path, working_dir: Path) -> Path:
    """
    accepts a tiff Path obj
    tests if 16 bit
    if 16 bit convert (write to assets_dir) & return Path
    else return orig Path
    """
    # first check if file extension is .tif or .tiff
    is_tiff_ext = False
    if fp.suffix.lower() == ".tiff" or fp.suffix.lower() == ".tif":
        is_tiff_ext = True
    if not is_tiff_ext:
        return fp

    if is_int16(fp):
        new_fp = Path(f"{working_dir.as_posix()}/{fp.name}")
        file_to_uint8(in_file_path=fp, out_file_path=str(new_fp))
        return new_fp
    else:
        return fp


@task
def create_dm2mrc_command(dm_fp: Path, output_fp: Path) -> str:
    cmd = f"{Config.dm2mrc_loc} {dm_fp} {output_fp} &> {output_fp.parent}/dm2mrc.log"
    utils.log(f"Generated cmd {cmd}")
    return cmd


@task
def create_jpeg_cmd(mrc_fp: Path, output_fp: Path) -> str:
    cmd = f"{Config.mrc2tif_loc} -j {mrc_fp} {output_fp} &> {output_fp.parent}/mrc2tif.log"
    utils.log(f"Generated cmd {cmd}")
    return cmd


@task
def create_gm_cmd(fp_in: Path, fp_out: Path, size: str) -> str:
    # gm convert
    # -size 300x300 "/io/20210525_1416_A000_G000.jpeg"
    # -resize 300x300 -sharpen 2 -quality 70 "/io/20210525_1416_A000_G000_sm.jpeg"
    if size == "sm":
        scaler = Config.size_sm
    elif size == "lg":
        scaler = Config.size_lg
    else:
        raise signals.FAIL(f"Thumbnail must be either sm or lg, not {size}")
    cmd = f"gm convert -size {scaler} {fp_in} -resize {scaler} -sharpen 2 -quality 70 {fp_out}"
    utils.log(f"Generated cmd {cmd}")
    return cmd


@task
def join_list(list1: List[Path], list2: List[Path]) -> List[Path]:
    return list1 + list2


@task
def dump_to_json(elt):
    print(json.dumps(elt))


with Flow(
    "dm_to_jpeg",
    state_handlers=[utils.notify_api_completion, utils.notify_api_running],
    executor=Config.SLURM_EXECUTOR,
    run_config=LocalRun(labels=[utils.get_environment()]),
) as flow:
    """
    [dm4 and dm3 inputs] ---> [mrc intermediary files] ---> [jpeg outputs]    -->
                                                            [add jpeg inputs] -->

    --> ALL scaled into sizes "sm" and "lg"
    """
    input_dir = Parameter("input_dir")
    file_name = Parameter("file_name", default=None)
    callback_url = Parameter("callback_url")()
    token = Parameter("token")()
    input_dir_fp = utils.get_input_dir(input_dir=input_dir)

    # [dm4 and dm3 inputs]
    dm_fps = utils.list_files(
        input_dir_fp, ["DM4", "DM3", "dm4", "dm3"], single_file=file_name
    )
    # one dir per input dm file
    working_dir_dms = utils.set_up_work_env.map(input_fp=dm_fps)

    # other input types need to be converted too.
    # Hopefully there's no naming overlaps (eg same_name.jpeg and same_name.dm4)
    other_input_fps = utils.list_files(
        input_dir=input_dir_fp,
        exts=["TIF", "TIFF", "JPEG", "PNG", "JPG", "tif", "tiff", "jpeg", "png", "jpg"],
        single_file=file_name,
    )
    working_dir_others = utils.set_up_work_env.map(input_fp=other_input_fps)

    # cat all files into single list, check they exist
    all_fps = join_list(dm_fps, other_input_fps)
    utils.check_inputs_ok(all_fps)

    int16_tiff_converted = convert_if_int16_tiff.map(
        fp=other_input_fps, working_dir=working_dir_others
    )
    # sanitize input file names
    # sanitized files are the starting point dir, Project files are not touched, need
    # to use that fname to init pipeline.
    # ONLY use sanitized inputs when need to use Projects files as inputs directly.
    # (else use the orig fnames, and gen_output_fp will TRANSLATE bad chars.
    dm_fps_sanitized = utils.sanitize_file_names(dm_fps)
    other_input_fps_sanitized = utils.sanitize_file_names(int16_tiff_converted)
    # have to convert int16 tiff files. Test and swap any int16 files found.

    # dm* to mrc conversion
    mrc_fps = utils.gen_output_fp.map(
        input_fp=dm_fps, working_dir=working_dir_dms, output_ext=unmapped(".mrc")
    )
    dm2mrc_commands = create_dm2mrc_command.map(
        dm_fp=dm_fps_sanitized, output_fp=mrc_fps
    )
    dm2mrcs = shell_task.map(
        command=dm2mrc_commands, to_echo=unmapped("running dm2mrc commands")
    )

    # mrc is intermed format, to jpeg conversion
    jpeg_fps = utils.gen_output_fp.map(input_fp=mrc_fps, output_ext=unmapped(".jpeg"))
    jpeg_commands = create_jpeg_cmd.map(mrc_fp=mrc_fps, output_fp=jpeg_fps)
    jpegs = shell_task.map(
        command=jpeg_commands,
        to_echo=unmapped("running jpeg commands"),
        upstream_tasks=[dm2mrcs],
    )

    # need to scale the newly created jpegs
    jpeg_fps_sm_fps = utils.gen_output_fp.map(
        input_fp=jpeg_fps, upstream_tasks=[jpegs], output_ext=unmapped("_SM.jpeg")
    )
    jpeg_fps_sm_cmds = create_gm_cmd.map(
        fp_in=jpeg_fps, fp_out=jpeg_fps_sm_fps, size=unmapped("sm")
    )
    jpeg_fps_sms = shell_task.map(command=jpeg_fps_sm_cmds)

    # need to scale the newly created jpegs
    jpeg_fps_lg_fps = utils.gen_output_fp.map(
        input_fp=jpeg_fps, upstream_tasks=[jpegs], output_ext=unmapped("_LG.jpeg")
    )
    jpeg_fps_lg_cmds = create_gm_cmd.map(
        fp_in=jpeg_fps, fp_out=jpeg_fps_lg_fps, size=unmapped("lg")
    )
    jpeg_fps_lgs = shell_task.map(command=jpeg_fps_lg_cmds)
    # an input dir can also contain tif / tiff / jpeg or png files

    # files in input dir, need to use working_dir to path
    other_input_sm_fps = utils.gen_output_fp.map(
        input_fp=other_input_fps,
        output_ext=unmapped("_SM.jpeg"),
        working_dir=working_dir_others,
    )
    other_input_gm_sm_cmds = create_gm_cmd.map(
        fp_in=other_input_fps_sanitized,
        fp_out=other_input_sm_fps,
        size=unmapped("sm"),
    )
    other_sm_gms = shell_task.map(command=other_input_gm_sm_cmds)

    # large thumbs
    other_input_lg_fps = utils.gen_output_fp.map(
        input_fp=other_input_fps,
        output_ext=unmapped("_LG.jpeg"),
        working_dir=working_dir_others,
    )
    other_input_lg_cmds = create_gm_cmd.map(
        fp_in=other_input_fps_sanitized, fp_out=other_input_lg_fps, size=unmapped("lg")
    )
    other_input_lgs = shell_task.map(command=other_input_lg_cmds)

    # At this point computation of the workflow is complete.
    # now need to copy subset of files to "Assets" dir
    # and
    # create a datastructure containing each of locations of the files.

    # dm3/dm4 type inputs (ie inputs we converted to jpegs)
    dm_primary_file_elts = utils.gen_callback_elt.map(input_fname=dm_fps)

    # create an assets_dir (to copy required outputs into)
    assets_dir_dm = utils.make_assets_dir(input_dir=input_dir_fp)
    assets_dir_other = utils.make_assets_dir(input_dir=input_dir_fp)

    # small thumbnails
    jpeg_fps_sm_asset_fps = utils.copy_to_assets_dir.map(
        fp=jpeg_fps_sm_fps,
        assets_dir=unmapped(assets_dir_dm),
        upstream_tasks=[jpeg_fps_sms],
    )
    with_dm_sm_thumbs = utils.add_assets_entry.map(
        base_elt=dm_primary_file_elts,
        path=jpeg_fps_sm_asset_fps,
        asset_type=unmapped("thumbnail"),
    )
    # finished small thumbnails

    # large thumbnails
    jpeg_fps_lg_asset_fps = utils.copy_to_assets_dir.map(
        fp=jpeg_fps_lg_fps,
        assets_dir=unmapped(assets_dir_dm),
        upstream_tasks=[jpeg_fps_lgs],
    )
    with_dm_lg_thumbs = utils.add_assets_entry.map(
        base_elt=with_dm_sm_thumbs,
        path=jpeg_fps_lg_asset_fps,
        asset_type=unmapped("keyImage"),
    )
    # finished large thumbnails

    # any other input that wasn't dm4
    other_primary_file_elts = utils.gen_callback_elt.map(input_fname=other_input_fps)

    # small thumbnails - other inputs
    other_assets_sm_fps = utils.copy_to_assets_dir.map(
        fp=other_input_sm_fps,
        assets_dir=unmapped(assets_dir_other),
        upstream_tasks=[other_sm_gms],
    )
    other_with_sm_thumbs = utils.add_assets_entry.map(
        base_elt=other_primary_file_elts,
        path=other_assets_sm_fps,
        asset_type=unmapped("thumbnail"),
    )
    # finished small thumbnails - other inputs

    # other inputs, large thumbs
    other_assets_lg_fps = utils.copy_to_assets_dir.map(
        fp=other_input_lg_fps,
        assets_dir=unmapped(assets_dir_other),
        upstream_tasks=[other_input_lgs],
    )
    other_with_lg_thumbs = utils.add_assets_entry.map(
        base_elt=other_with_sm_thumbs,
        path=other_assets_lg_fps,
        asset_type=unmapped("keyImage"),
    )
    # finished with other input conversion.

    all_assets = join_list(with_dm_lg_thumbs, other_with_lg_thumbs)

    cp_other = utils.cp_logs_to_assets.map(
        working_dir=working_dir_others,
        assets_dir=unmapped(assets_dir_other),
        upstream_tasks=[unmapped(all_assets)],
    )
    cp_dm = utils.cp_logs_to_assets.map(
        working_dir=working_dir_dms,
        assets_dir=unmapped(assets_dir_dm),
        upstream_tasks=[unmapped(all_assets)],
    )

    cb = utils.send_callback_body(
        token=token, callback_url=callback_url, files_elts=all_assets
    )

    utils.cleanup_workdir.map(wd=working_dir_dms, upstream_tasks=[cp_dm, cp_dm])
    utils.cleanup_workdir.map(wd=working_dir_others, upstream_tasks=[cp_dm, cp_other])