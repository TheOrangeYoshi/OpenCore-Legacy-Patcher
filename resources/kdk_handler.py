# Kernel Debug Kit downloader

import datetime
import re
import urllib.parse
from pathlib import Path
from typing import cast

import packaging.version
import requests

import subprocess

import logging

from resources import utilities
from resources.constants import Constants


class kernel_debug_kit_handler:
    def __init__(self, constants: Constants):
        self.constants = constants

    def get_available_kdks(self):
        KDK_API_LINK = "https://kdk-api.dhinak.net/v1"

        logging.info("- Fetching available KDKs")

        try:
            results = utilities.SESSION.get(KDK_API_LINK, headers={"User-Agent": f"OCLP/{self.constants.patcher_version}"}, timeout=10)
        except (requests.exceptions.Timeout, requests.exceptions.TooManyRedirects, requests.exceptions.ConnectionError):
            logging.info("- Could not contact KDK API")
            return None

        if results.status_code != 200:
            logging.info("- Could not fetch KDK list")
            return None

        return sorted(results.json(), key=lambda x: (packaging.version.parse(x["version"]), datetime.datetime.fromisoformat(x["date"])), reverse=True)

    def get_closest_match_legacy(self, host_version: str, host_build: str):
        # Get the closest match to the provided version
        # KDKs are generally a few days late, so we'll rely on N-1 matching

        # Note: AppleDB is manually updated, so this is not a perfect solution

        OS_DATABASE_LINK = "https://api.appledb.dev/main.json"
        VERSION_PATTERN = re.compile(r"\d+\.\d+(\.\d+)?")

        parsed_host_version = cast(packaging.version.Version, packaging.version.parse(host_version))

        logging.info(f"- Checking closest match for: {host_version} build {host_build}")

        try:
            results = utilities.SESSION.get(OS_DATABASE_LINK)
        except (requests.exceptions.Timeout, requests.exceptions.TooManyRedirects, requests.exceptions.ConnectionError):
            logging.info("- Could not contact AppleDB")
            return None, "", ""

        if results.status_code != 200:
            logging.info("- Could not fetch database")
            return None, "", ""

        macos_builds = [i for i in results.json()["ios"] if i["osType"] == "macOS"]
        # If the version is borked, put it at the bottom of the list
        # Would omit it, but can't do that in this lambda
        macos_builds.sort(key=lambda x: (packaging.version.parse(VERSION_PATTERN.match(x["version"]).group() if VERSION_PATTERN.match(x["version"]) else "0.0.0"), datetime.datetime.fromisoformat(x["released"] if x["released"] != "" else "1984-01-01")), reverse=True)  # type: ignore

        # Iterate through, find build that is closest to the host version
        # Use date to determine which is closest
        for build_info in macos_builds:
            if build_info["osType"] == "macOS":
                raw_version = VERSION_PATTERN.match(build_info["version"])
                if not raw_version:
                    # Skip if version is borked
                    continue
                version = cast(packaging.version.Version, packaging.version.parse(raw_version.group()))
                build = build_info["build"]
                if build == host_build:
                    # Skip, as we want the next closest match
                    continue
                elif version <= parsed_host_version and version.major == parsed_host_version.major and version.minor == parsed_host_version.minor:
                    # The KDK list is already sorted by date then version, so the first match is the closest
                    logging.info(f"- Closest match: {version} build {build}")
                    return self.generate_kdk_link(str(version), build), str(version), build

        logging.info("- Could not find a match")
        return None, "", ""

    def generate_kdk_link(self, version: str, build: str):
        return f"https://download.developer.apple.com/macOS/Kernel_Debug_Kit_{version}_build_{build}/Kernel_Debug_Kit_{version}_build_{build}.dmg"

    def verify_apple_developer_portal(self, link):
        # Determine whether Apple Developer Portal is up
        # and if the requested file is available

        # Returns following:
        # 0: Portal is up and file is available
        # 1: Portal is up but file is not available
        # 2: Portal is down
        # 3: Network error

        if utilities.verify_network_connection("https://developerservices2.apple.com/services/download") is False:
            logging.info("- Could not connect to the network")
            return 3

        TOKEN_URL_BASE = "https://developerservices2.apple.com/services/download"
        remote_path = urllib.parse.urlparse(link).path
        token_url = urllib.parse.urlunparse(urllib.parse.urlparse(TOKEN_URL_BASE)._replace(query=urllib.parse.urlencode({"path": remote_path})))

        try:
            response = utilities.SESSION.get(token_url, timeout=5)
        except (requests.exceptions.Timeout, requests.exceptions.TooManyRedirects, requests.exceptions.ConnectionError):
            logging.info("- Could not contact Apple download servers")
            return 2

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            if response.status_code == 400 and "The path specified is invalid" in response.text:
                logging.info("- File does not exist on Apple download servers")
                return 1
            else:
                logging.info("- Could not request download authorization from Apple download servers")
                return 2
        return 0

    def download_kdk(self, version: str, build: str):
        detected_build = build

        if self.is_kdk_installed(detected_build) is True:
            logging.info("- KDK is already installed")
            self.remove_unused_kdks(exclude_builds=[detected_build])
            return True, "", detected_build

        download_link = None
        closest_match_download_link = None
        closest_version = ""
        closest_build = ""

        kdk_list = self.get_available_kdks()

        parsed_version = cast(packaging.version.Version, packaging.version.parse(version))

        if kdk_list:
            for kdk in kdk_list:
                kdk_version = cast(packaging.version.Version, packaging.version.parse(kdk["version"]))
                if kdk["build"] == build:
                    download_link = kdk["url"]
                elif not closest_match_download_link and kdk_version <= parsed_version and kdk_version.major == parsed_version.major and (kdk_version.minor == parsed_version.minor or kdk_version.minor == parsed_version.minor - 1):
                    # The KDK list is already sorted by date then version, so the first match is the closest
                    closest_match_download_link = kdk["url"]
                    closest_version = kdk["version"]
                    closest_build = kdk["build"]
        else:
            logging.info("- Could not fetch KDK list, falling back to brute force")
            download_link = self.generate_kdk_link(version, build)
            closest_match_download_link, closest_version, closest_build = self.get_closest_match_legacy(version, build)

        logging.info(f"- Checking for KDK matching macOS {version} build {build}")
        # download_link is None if no matching KDK is found, so we'll fall back to the closest match
        result = self.verify_apple_developer_portal(download_link) if download_link else 1
        if result == 0:
            logging.info("- Downloading KDK")
        elif result == 1:
            logging.info("- Could not find KDK, finding closest match")

            if self.is_kdk_installed(closest_build) is True:
                logging.info(f"- Closet Build ({closest_build}) already installed")
                self.remove_unused_kdks(exclude_builds=[detected_build, closest_build])
                return True, "", closest_build

            if closest_match_download_link is None:
                msg = "Could not find KDK for host, nor closest match"
                logging.info(f"- {msg}")
                return False, msg, ""

            logging.info(f"- Closest match: {closest_version} build {closest_build}")
            result = self.verify_apple_developer_portal(closest_match_download_link)

            if result == 0:
                logging.info("- Downloading KDK")
                download_link = closest_match_download_link
            elif result == 1:
                msg = "Could not find KDK for host on Apple's servers, nor closest match"
                logging.info(f"- {msg}")
                return False, msg, ""
            elif result == 2:
                msg = "Could not contact Apple download servers"
                download_link = self.kdk_backup_site(closest_build)
                if download_link is None:
                    msg += " and could not find a backup copy online"
                    logging.info(f"- {msg}")
                    return False, msg, ""
            else:
                msg = "Unknown error"
                logging.info(f"- {msg}")
                return False, msg, ""
        elif result == 2:
            msg = "Could not contact Apple download servers"
            download_link = self.kdk_backup_site(build)
            if download_link is None:
                msg += " and could not find a backup copy online"
                logging.info(f"- {msg}")
                return False, msg, ""
        elif result == 3:
            msg = "Failed to connect to the internet"
            logging.info(f"- {msg}")
            return False, msg, ""

        if "github" in download_link:
            result = utilities.download_file(download_link, self.constants.kdk_download_path)
        else:
            result = utilities.download_apple_developer_portal(download_link, self.constants.kdk_download_path)

        if result:
            result = subprocess.run(["hdiutil", "verify", self.constants.kdk_download_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                logging.info(f"Error: Kernel Debug Kit checksum verification failed!")
                logging.info(f"Output: {result.stderr}")
                msg = "Kernel Debug Kit checksum verification failed, please try again.\n\nIf this continues to fail, ensure you're downloading on a stable network connection (ie. Ethernet)"
                logging.info(f"- {msg}")
                return False, msg, ""
            self.remove_unused_kdks(exclude_builds=[detected_build, closest_build])
            return True, "", detected_build
        msg = "Failed to download KDK"
        logging.info(f"- {msg}")
        return False, msg, ""

    def is_kdk_installed(self, build):
        kexts_to_check = [
            "System.kext/PlugIns/Libkern.kext/Libkern",
            "apfs.kext/Contents/MacOS/apfs",
            "IOUSBHostFamily.kext/Contents/MacOS/IOUSBHostFamily",
            "AMDRadeonX6000.kext/Contents/MacOS/AMDRadeonX6000",
        ]

        if Path("/Library/Developer/KDKs").exists():
            for file in Path("/Library/Developer/KDKs").iterdir():
                if file.is_dir():
                    if file.name.endswith(f"{build}.kdk"):
                        for kext in kexts_to_check:
                            if not Path(f"{file}/System/Library/Extensions/{kext}").exists():
                                logging.info(f"- Corrupted KDK found, removing due to missing: {file}/System/Library/Extensions/{kext}")
                                utilities.elevated(["rm", "-rf", file], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                                return False
                        return True
        return False

    def remove_unused_kdks(self, exclude_builds=[]):
        if self.constants.should_nuke_kdks is False:
            return

        if not Path("/Library/Developer/KDKs").exists():
            return

        if exclude_builds == []:
            return

        logging.info("- Cleaning unused KDKs")
        for kdk_folder in Path("/Library/Developer/KDKs").iterdir():
            if kdk_folder.is_dir():
                if kdk_folder.name.endswith(".kdk"):
                    should_remove = True
                    for build in exclude_builds:
                        if build != "" and kdk_folder.name.endswith(f"{build}.kdk"):
                            should_remove = False
                            break
                    if should_remove is False:
                        continue
                    logging.info(f"  - Removing {kdk_folder.name}")
                    utilities.elevated(["rm", "-rf", kdk_folder], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


    def kdk_backup_site(self, build):
        KDK_MIRROR_REPOSITORY = "https://api.github.com/repos/dortania/KdkSupportPkg/releases"

        # Check if tag exists
        catalog = requests.get(KDK_MIRROR_REPOSITORY)
        if catalog.status_code != 200:
            logging.info(f"- Could not contact KDK mirror repository")
            return None

        catalog = catalog.json()

        for release in catalog:
            if release["tag_name"] == build:
                logging.info(f"- Found KDK mirror for build: {build}")
                for asset in release["assets"]:
                    if asset["name"].endswith(".dmg"):
                        return asset["browser_download_url"]

        logging.info(f"- Could not find KDK mirror for build {build}")
        return None