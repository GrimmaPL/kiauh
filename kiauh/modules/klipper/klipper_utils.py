#!/usr/bin/env python3

# ======================================================================= #
#  Copyright (C) 2020 - 2023 Dominik Willner <th33xitus@gmail.com>        #
#                                                                         #
#  This file is part of KIAUH - Klipper Installation And Update Helper    #
#  https://github.com/dw-0/kiauh                                          #
#                                                                         #
#  This file may be distributed under the terms of the GNU GPLv3 license  #
# ======================================================================= #

import os
import re
import grp
import shutil
import subprocess
import textwrap
from pathlib import Path

from typing import List, Union, Literal, Dict

from kiauh.core.config_manager.config_manager import ConfigManager
from kiauh.core.instance_manager.base_instance import BaseInstance
from kiauh.core.instance_manager.instance_manager import InstanceManager
from kiauh.core.instance_manager.name_scheme import NameScheme
from kiauh.modules.klipper import MODULE_PATH, KLIPPER_DIR, KLIPPER_ENV_DIR
from kiauh.modules.klipper.klipper import Klipper
from kiauh.modules.klipper.klipper_dialogs import (
    print_missing_usergroup_dialog,
    print_instance_overview,
    print_select_instance_count_dialog,
    print_select_custom_name_dialog,
)
from kiauh.modules.moonraker.moonraker import Moonraker
from kiauh.modules.moonraker.moonraker_utils import moonraker_to_multi_conversion
from kiauh.utils.common import get_install_status_common, get_repo_name
from kiauh.utils.constants import CURRENT_USER
from kiauh.utils.input_utils import get_confirm, get_string_input, get_number_input
from kiauh.utils.logger import Logger
from kiauh.utils.system_utils import mask_system_service


def get_klipper_status() -> Dict[Literal["status", "repo"], str]:
    return {
        "status": get_install_status_common(Klipper, KLIPPER_DIR, KLIPPER_ENV_DIR),
        "repo": get_repo_name(KLIPPER_DIR),
    }


def check_is_multi_install(
    existing_instances: List[Klipper], install_count: int
) -> bool:
    return not existing_instances and install_count > 1


def check_is_single_to_multi_conversion(existing_instances: List[Klipper]) -> bool:
    return len(existing_instances) == 1 and existing_instances[0].suffix == ""


def init_name_scheme(
    existing_instances: List[Klipper], install_count: int
) -> NameScheme:
    if check_is_multi_install(
        existing_instances, install_count
    ) or check_is_single_to_multi_conversion(existing_instances):
        print_select_custom_name_dialog()
        if get_confirm("Assign custom names?", False, allow_go_back=True):
            return NameScheme.CUSTOM
        else:
            return NameScheme.INDEX
    else:
        return NameScheme.SINGLE


def update_name_scheme(
    name_scheme: NameScheme,
    name_dict: Dict[int, str],
    klipper_instances: List[Klipper],
    moonraker_instances: List[Moonraker],
) -> NameScheme:
    # if there are more moonraker instances installed than klipper, we
    # load their names into the name_dict, as we will detect and enforce that naming scheme
    if len(moonraker_instances) > len(klipper_instances):
        update_name_dict(name_dict, moonraker_instances)
        return detect_name_scheme(moonraker_instances)
    elif len(klipper_instances) > 1:
        update_name_dict(name_dict, klipper_instances)
        return detect_name_scheme(klipper_instances)
    else:
        return name_scheme


def update_name_dict(name_dict: Dict[int, str], instances: List[BaseInstance]) -> None:
    for k, v in enumerate(instances):
        name_dict[k] = v.suffix


def handle_instance_naming(name_dict: Dict[int, str], name_scheme: NameScheme) -> None:
    if name_scheme == NameScheme.SINGLE:
        return

    for k in name_dict:
        if name_dict[k] == "" and name_scheme == NameScheme.INDEX:
            name_dict[k] = str(k + 1)
        elif name_dict[k] == "" and name_scheme == NameScheme.CUSTOM:
            assign_custom_name(k, name_dict)


def add_to_existing() -> bool:
    kl_instances = InstanceManager(Klipper).instances
    print_instance_overview(kl_instances)
    return get_confirm("Add new instances?", allow_go_back=True)


def get_install_count() -> Union[int, None]:
    """
    Print a dialog for selecting the amount of Klipper instances
    to set up with an option to navigate back. Returns None if the
    user selected to go back, otherwise an integer greater or equal than 1 |
    :return: Integer >= 1 or None
    """
    kl_instances = InstanceManager(Klipper).instances
    print_select_instance_count_dialog()
    question = f"Number of{' additional' if len(kl_instances) > 0 else ''} Klipper instances to set up"
    return get_number_input(question, 1, default=1, allow_go_back=True)


def assign_custom_name(key: int, name_dict: Dict[int, str]) -> None:
    existing_names = []
    existing_names.extend(Klipper.blacklist())
    existing_names.extend(name_dict[n] for n in name_dict)
    question = f"Enter name for instance {key + 1}"
    name_dict[key] = get_string_input(question, exclude=existing_names)


def handle_to_multi_instance_conversion(new_name: str) -> None:
    Logger.print_status("Converting single instance to multi instances ...")
    klipper_to_multi_conversion(new_name)
    moonraker_to_multi_conversion(new_name)


def klipper_to_multi_conversion(new_name: str) -> None:
    Logger.print_status("Convert Klipper single to multi instance ...")
    im = InstanceManager(Klipper)
    im.current_instance = im.instances[0]
    # temporarily store the data dir path
    old_data_dir = im.instances[0].data_dir
    # remove the old single instance
    im.stop_instance()
    im.disable_instance()
    im.delete_instance()
    # create a new klipper instance with the new name
    im.current_instance = Klipper(suffix=new_name)
    new_data_dir: Path = im.current_instance.data_dir

    # rename the old data dir and use it for the new instance
    Logger.print_status(f"Rename '{old_data_dir}' to '{new_data_dir}' ...")
    if not new_data_dir.is_dir():
        old_data_dir.rename(new_data_dir)
    else:
        Logger.print_info(f"'{new_data_dir}' already exist. Skipped ...")

    im.create_instance()
    im.enable_instance()
    im.start_instance()


def check_user_groups():
    current_groups = [grp.getgrgid(gid).gr_name for gid in os.getgroups()]

    missing_groups = []
    if "tty" not in current_groups:
        missing_groups.append("tty")
    if "dialout" not in current_groups:
        missing_groups.append("dialout")

    if not missing_groups:
        return

    print_missing_usergroup_dialog(missing_groups)
    if not get_confirm(f"Add user '{CURRENT_USER}' to group(s) now?"):
        log = "Skipped adding user to required groups. You might encounter issues."
        Logger.warn(log)
        return

    try:
        for group in missing_groups:
            Logger.print_status(f"Adding user '{CURRENT_USER}' to group {group} ...")
            command = ["sudo", "usermod", "-a", "-G", group, CURRENT_USER]
            subprocess.run(command, check=True)
            Logger.print_ok(f"Group {group} assigned to user '{CURRENT_USER}'.")
    except subprocess.CalledProcessError as e:
        Logger.print_error(f"Unable to add user to usergroups: {e}")
        raise

    log = "Remember to relog/restart this machine for the group(s) to be applied!"
    Logger.print_warn(log)


def handle_disruptive_system_packages() -> None:
    services = []

    command = ["systemctl", "is-enabled", "brltty"]
    brltty_status = subprocess.run(command, capture_output=True, text=True)

    command = ["systemctl", "is-enabled", "brltty-udev"]
    brltty_udev_status = subprocess.run(command, capture_output=True, text=True)

    command = ["systemctl", "is-enabled", "ModemManager"]
    modem_manager_status = subprocess.run(command, capture_output=True, text=True)

    if "enabled" in brltty_status.stdout:
        services.append("brltty")
    if "enabled" in brltty_udev_status.stdout:
        services.append("brltty-udev")
    if "enabled" in modem_manager_status.stdout:
        services.append("ModemManager")

    for service in services if services else []:
        try:
            log = f"{service} service detected! Masking {service} service ..."
            Logger.print_status(log)
            mask_system_service(service)
            Logger.print_ok(f"{service} service masked!")
        except subprocess.CalledProcessError:
            warn_msg = textwrap.dedent(
                f"""
                KIAUH was unable to mask the {service} system service. 
                Please fix the problem manually. Otherwise, this may have 
                undesirable effects on the operation of Klipper.
                """
            )[1:]
            Logger.print_warn(warn_msg)


def detect_name_scheme(instance_list: List[BaseInstance]) -> NameScheme:
    pattern = re.compile("^\d+$")
    for instance in instance_list:
        if not pattern.match(instance.suffix):
            return NameScheme.CUSTOM

    return NameScheme.INDEX


def get_highest_index(instance_list: List[Klipper]) -> int:
    indices = [int(instance.suffix.split("-")[-1]) for instance in instance_list]
    return max(indices)


def create_example_printer_cfg(instance: Klipper) -> None:
    Logger.print_status(f"Creating example printer.cfg in '{instance.cfg_dir}'")
    if instance.cfg_file.is_file():
        Logger.print_info(f"'{instance.cfg_file}' already exists.")
        return

    source = MODULE_PATH.joinpath("res/printer.cfg")
    target = instance.cfg_file
    try:
        shutil.copy(source, target)
    except OSError as e:
        Logger.print_error(f"Unable to create example printer.cfg:\n{e}")
        return

    cm = ConfigManager(target)
    cm.set_value("virtual_sdcard", "path", str(instance.gcodes_dir))
    cm.write_config()
    Logger.print_ok(f"Example printer.cfg created in '{instance.cfg_dir}'")
