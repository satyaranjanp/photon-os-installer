#!/usr/bin/env python3

import os
import glob
import json
import shutil
import platform
import collections
from logger import Logger
from argparse import ArgumentParser
from commandutils import CommandUtils


class IsoBuilder(object):

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.pkg_list = []
        self.working_dir = os.path.join(self.artifact_path, "photon_iso")
        self.iso_name = os.path.join(self.artifact_path, f"photon-{self.photon_release_version}.iso")
        self.rpms_path = os.path.join(self.working_dir, "RPMS")
        self.initrd_path = os.path.join(self.working_dir, "photon-chroot")
        self.photon_docker_image = f"photon:{self.photon_release_version}"
        self.logger = Logger.get_logger(os.path.join(self.artifact_path, "LOGS"), self.log_level, True)
        self.cmdUtil = CommandUtils(self.logger)
        self.architecture = platform.machine()
        self.additional_files = []

    def runCmd(self, cmd):
        retval = self.cmdUtil.run(cmd)
        if retval:
            raise Exception(f"Following command failed to execute: {cmd}")


    def addPkgsToList(self, pkg_list_file):
        pkg_data = CommandUtils.jsonread(pkg_list_file)
        self.pkg_list.extend(pkg_data["packages"])
        if f"packages_{self.architecture}" in pkg_data:
            self.pkg_list.extend(pkg_data[f"packages_{self.architecture}"])


    def addGrubConfig(self):
        self.logger.info("Adding grub config...")
        if not os.path.exists(f"{self.working_dir}/boot/grub2"):
            self.logger.info(f"Creating grub dir: {self.working_dir}/boot/grub2")
            os.makedirs(f"{self.working_dir}/boot/grub2")
        with open(f"{self.working_dir}/boot/grub2/grub.cfg", "w") as conf_file:
            conf_file.writelines([
                "set default=0\n",
                "set timeout=3\n",
                'set gfxmode="1024x768"\n',
                "gfxpayload=keep\n",
                "set theme=/boot/grub2/themes/photon/theme.txt\n",
                "terminal_output gfxterm\n",
                "probe -s photondisk -u ($root)\n\n",
                'menuentry "Install" {\n',
                f"linux /isolinux/vmlinuz root=/dev/ram0 loglevel=3 photon.media=UUID=$photondisk {self.boot_cmdline_param}\n",
                "initrd /isolinux/initrd.img}"])


    def createInstallOptionJson(self):
        install_option_key = "custom"
        additional_files = [os.path.basename(file) for file in self.additional_files]
        if self.function == "build-rpm-ostree-iso":
            install_option_key = "ostree_host"
            additional_files = ["ostree-repo.tar.gz"]
        install_option_data = {
                                install_option_key: {
                                    "title": "Photon Custom",
                                    "packagelist_file": os.path.basename(self.custom_packages_json),
                                    "visible": False,
                                    "additional-files": additional_files
                                }
                            }
        with open(f"{self.working_dir}/build_install_options_custom.json", "w") as json_file:
            json_file.write(json.dumps(install_option_data))

    def generateInitrd(self):
        """
        Generate custom initrd
        """
        initrd_pkgs = None
        if not os.path.exists(self.working_dir):
            self.logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir)
        if not self.initrd_pkg_list_file:
            initrd_pkg_file = f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/common/data/packages_installer_initrd.json"
            self.logger.info(f"Downloading initrd package list file {initrd_pkg_file}...")
            self.cmdUtil.wget(initrd_pkg_file, f"{self.working_dir}/packages_installer_initrd.json")
            self.initrd_pkg_list_file = f"{self.working_dir}/packages_installer_initrd.json"
        initrd_pkg_data = CommandUtils.jsonread(self.initrd_pkg_list_file)
        initrd_pkgs = initrd_pkg_data["packages"]
        if f"packages_{self.architecture}" in initrd_pkg_data:
            initrd_pkgs.extend(
                initrd_pkg_data[f"packages_{self.architecture}"])
        self.logger.info(f"Initrd package list: {initrd_pkgs}")
        initrd_pkgs = " ".join(initrd_pkgs)

        # Download all initrd packages before installing them during initrd generation.
        # Skip downloading packages if ostree iso.
        ostree_iso = False
        if self.function != "build-rpm-ostree-iso":
            self.downloadPkgs()
        else:
            ostree_iso = True
            self.additional_files.append(self.ostree_tar_path)

        self.createInstallOptionJson()

        # Get absolute path of generate_initrd script
        initrd_script = f"{os.path.dirname(os.path.abspath(__file__))}/generate_initrd.sh"
        self.logger.info("Starting to generate initrd.img...")
        self.runCmd([initrd_script, self.working_dir, initrd_pkgs,
                     self.rpms_path, self.photon_release_version,
                     self.custom_packages_json, "build_install_options_custom.json", str(ostree_iso)])


    def downloadPkgs(self):
        if not os.path.exists(self.rpms_path):
            self.logger.info(f"Creating RPMS directory: {self.rpms_path}")
            os.makedirs(self.rpms_path)

        # Add installer initrd and custom packages to package list..
        self.addPkgsToList(self.initrd_pkg_list_file)
        self.addPkgsToList(self.custom_packages_json)

        linux_flavors = ["linux", "linux-esx", "linux-rt", "linux-aws", "linux-secure"]
        if not any(flavor in self.pkg_list for flavor in linux_flavors):
            self.pkg_list.append("linux")

        # Include additional packages if mentioned in kickstart.
        if self.kickstart_path:
            kickstart_data = CommandUtils.jsonread(self.kickstart_path)
            if "packages" in kickstart_data:
                self.pkg_list.extend(kickstart_data["packages"])

        pkg_list = " ".join(self.pkg_list)
        self.logger.info(f"List of packages to download: {self.pkg_list}")
        additionalRepo = ""
        if self.additional_repos:
            self.logger.info(f"List of additional repos given to download packages from: {self.additional_repos}")
            for repo in self.additional_repos:
                abs_repo_path = os.path.abspath(repo)
                additionalRepo += f"--mount  type=bind,source={abs_repo_path},target=/etc/yum.repos.d/{os.path.basename(repo)} "

        # TDNF cmd to download packages in the list from packages.vmware.com/photon.
        # Using --alldeps option to include all dependencies even though package might be installed on system.
        tdnf_download_cmd = (f"tdnf --releasever {self.photon_release_version} --alldeps --downloadonly -y "
                             f"--downloaddir={self.working_dir}/RPMS install {pkg_list}")
        download_cmd = (f"docker run --privileged --rm {additionalRepo} -v {self.rpms_path}:{self.rpms_path} "
                        f"-v {self.working_dir}:{self.working_dir} photon:{self.photon_release_version} "
                        f"/bin/bash -c \"tdnf clean all && tdnf update tdnf -y && {tdnf_download_cmd}\"")
        self.logger.info("Starting to download packages...")
        self.logger.debug(f"Starting to download packages:\n{download_cmd}")
        self.runCmd(download_cmd)

        # Seperate out packages downloaded into arch specific directories.
        # Run createrepo on the rpm download path once downloaded.
        if not os.path.exists(f"{self.rpms_path}/x86_64"):
            os.mkdir(f"{self.rpms_path}/x86_64")
        if not os.path.exists(f"{self.rpms_path}/noarch"):
            os.mkdir(f"{self.rpms_path}/noarch")
        for file in os.listdir(f"{self.working_dir}/RPMS"):
            if file.endswith('.x86_64.rpm'):
                shutil.move(f"{self.rpms_path}/{file}",
                            f"{self.rpms_path}/x86_64/{file}")
            elif file.endswith('.noarch.rpm'):
                shutil.move(f"{self.rpms_path}/{file}",
                            f"{self.rpms_path}/noarch/{file}")
        self.logger.info("Creating repodata for downloaded packages...")
        self.createRepo()


    def createRepo(self):
        repoDataDir = f"{self.rpms_path}/repodata"
        self.runCmd(f"createrepo --database --update {self.rpms_path}")
        if os.path.exists(repoDataDir):
            primary_xml_gz = glob.glob(repoDataDir + "/*primary.xml.gz")
            self.runCmd(f"ln -sfv {primary_xml_gz[0]} {repoDataDir}/primary.xml.gz")


    def cleanUp(self):
        try:
            shutil.rmtree(self.working_dir)
        except FileNotFoundError:
            pass


    def createEfiImg(self):
        """
        create efi image
        """
        self.logger.info("Creating EFI image...")
        self.efi_img = "boot/grub2/efiboot.img"
        efi_dir = os.path.join(self.artifact_path, "efiboot")
        self.runCmd(f"dd if=/dev/zero of={self.working_dir}/{self.efi_img} bs=3K count=1024")
        self.runCmd(f"mkdosfs {self.working_dir}/{self.efi_img}")
        os.makedirs(efi_dir)
        self.runCmd(f"mount -o loop {self.working_dir}/{self.efi_img} {efi_dir}")
        shutil.move(f"{self.working_dir}/boot/efi/EFI", efi_dir)
        os.listdir(efi_dir)
        self.runCmd(f"umount {efi_dir}")
        shutil.rmtree(efi_dir)


    def createIsolinux(self):
        """
        Install photon-iso-config rpm in working dir.
        """
        if not os.path.exists(f"{self.working_dir}/isolinux"):
            os.makedirs(f"{self.working_dir}/isolinux")
        shutil.move(f"{self.working_dir}/initrd.img", f"{self.working_dir}/isolinux")

        self.logger.info("Installing photon-iso-config and syslinux in working directory...")
        os.makedirs(f"{self.working_dir}/isolinux-temp")
        pkg_list=["photon-iso-config"]
        if self.architecture == "x86_64":
            pkg_list.extend(["syslinux"])
        pkg_list = " ".join(pkg_list)
        tdnf_install_cmd = (f"tdnf install -qy --releasever {self.photon_release_version} --installroot {self.working_dir}/isolinux-temp "
                            f"--rpmverbosity 10 {pkg_list}")

        self.logger.debug(tdnf_install_cmd)
        # When using tdnf --installroot or rpm --root on chroot folder without /proc mounted, we must limit number of open files
        # to avoid librpm hang scanning all possible FDs.
        self.runCmd((f'docker run --privileged --ulimit nofile=1024:1024 --rm -v {self.working_dir}:{self.working_dir}'
                    f' photon:{self.photon_release_version} /bin/bash -c "{tdnf_install_cmd}"'))

        self.logger.debug("Succesfully installed photon-iso-config syslinux...")
        for file in os.listdir(f"{self.working_dir}/isolinux-temp/usr/share/photon-iso-config"):
            shutil.copyfile(f"{self.working_dir}/isolinux-temp/usr/share/photon-iso-config/{file}", f"{self.working_dir}/isolinux/{file}")
        for file in ["isolinux.bin", "libcom32.c32", "libutil.c32", "vesamenu.c32", "ldlinux.c32"]:
            shutil.copyfile(f"{self.working_dir}/isolinux-temp/usr/share/syslinux/{file}", f"{self.working_dir}/isolinux/{file}")
        shutil.rmtree(f"{self.working_dir}/isolinux-temp")
        for file in ["tdnf.conf", "photon-local.repo"]:
            if os.path.exists(f"{self.working_dir}/{file}"):
                os.remove(f"{self.working_dir}/{file}")
        shutil.move(f"{self.working_dir}/sample_ks.cfg", f"{self.working_dir}/isolinux")
        if self.kickstart_path:
            self.logger.info(f"Moving {self.kickstart_path} to {self.working_dir}/isolinux...")
            shutl.copyfile(f"{self.kickstart_path}", f"{self.working_dir}/isolinux")
        if self.boot_cmdline_param:
            self.logger.info("Adding Boot command line paramters to isolinux menu...")
            self.runCmd(f"sed -i '/photon.media=cdrom/ s#$# {self.boot_cmdline_param}#' {self.working_dir}/isolinux/menu.cfg")


    def copyAdditionalFiles(self):
        for file in self.additional_files:
            shutil.copy(file, self.working_dir)
            # Rename ostree tar to ostree-repo.tar.gz in iso.
            if file == self.ostree_tar_path and os.path.basename(self.ostree_tar_path) != "ostree-repo.tar.gz":
                shutil.move(f"{self.working_dir}/{os.path.basename(self.ostree_tar_path)}", f"{self.working_dir}/ostree-repo.tar.gz")


    def build(self):
        """
        Create Custom Iso
        """
        # Clean up
        self.logger.info(f"Cleaning up working directory: {self.working_dir}")
        self.cleanUp()

        if not os.path.exists(self.working_dir):
            self.logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir)

        # Download open source license for given branch and extract it in working dir.
        files_to_download = [f"https://github.com/vmware/photon/raw/{self.photon_release_version}/support/image-builder/iso/open_source_license.tar.gz",
                             f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/support/image-builder/iso/sample_ks.cfg",
                             f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/support/image-builder/iso/sample_ui.cfg",
                             f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/NOTICE-Apachev2",
                             f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/NOTICE-GPL2.0",
                             f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/EULA.txt"]

        # Download ostree tar to working directory if url is provided.
        if self.ostree_tar_path.startswith("http"):
            files_to_download.append(self.ostree_tar_path)
            self.ostree_tar_path = f"{self.working_dir}/{os.path.basename(self.ostree_tar_path)}"

        for file in files_to_download:
            self.logger.info(f"Downloading file: {file}")
            self.cmdUtil.wget(file, f'{self.working_dir}/{os.path.basename(file)}')

        # Generating Initrd img.
        self.generateInitrd()

        # Create isolinux dir inside iso.
        self.createIsolinux()

        # Copy Additional Files
        # In case of ostree-iso copy ostree tar to working directory.
        self.copyAdditionalFiles()

        self.createEfiImg()
        self.runCmd(f"mv {self.working_dir}/boot/vmlinuz* {self.working_dir}/isolinux/vmlinuz")

        # ID in the initrd.gz now is PHOTON_VMWARE_CD . This is how we recognize that the cd is actually ours. touch this file there.
        self.runCmd(f"touch {self.working_dir}/PHOTON_VMWARE_CD")

        self.addGrubConfig()

        self.logger.info(f"Generating Iso: {self.iso_name}")
        build_iso_cmd = f"pushd {self.working_dir} && "
        build_iso_cmd += "mkisofs -R -l -L -D -b isolinux/isolinux.bin -c isolinux/boot.cat "
        build_iso_cmd += "-no-emul-boot -boot-load-size 4 -boot-info-table "
        build_iso_cmd += f"-eltorito-alt-boot -e {self.efi_img} -no-emul-boot "
        build_iso_cmd += f"-V \"PHOTON_$(date +%Y%m%d)\" {self.working_dir} > {self.iso_name} && "
        build_iso_cmd += "popd"
        self.runCmd(build_iso_cmd)


def main():
    usage = "Usage: %prog [options]"
    parser = ArgumentParser(usage)
    parser.add_argument("-l", "--log-level", dest="log_level", default="info")
    parser.add_argument("-f", "--function", dest="function", default="build-iso", help="Building Options", choices=["build-iso", "build-initrd", "build-rpm-ostree-iso"])
    parser.add_argument("-v", "--photon-release-version", dest="photon_release_version", required=True, help="Photon release version to build custom iso/initrd.")
    parser.add_argument("-o", "--ostree-tar-path", dest="ostree_tar_path", default="", help="Path to custom ostree tar.")
    parser.add_argument("-c", "--custom-initrd-pkgs", dest="custom_initrd_pkgs", default=None, help="<Optional> parameter to provide cutom initrd pkg list file.")
    parser.add_argument("-r", "--additional_repos", action="append", default=None, help="<Optional> Pass repo file as input to download rpms from external repo")
    parser.add_argument("-p", "--custom-packages-json", dest="custom_packages_json", default="", help="Custom package list file.")
    parser.add_argument("-k", "--kickstart-path", dest="kickstart_path", default=None, help="<Optional> Path to custom kickstart file.")
    parser.add_argument("-b", "--boot-cmdline-param", dest="boot_cmdline_param", default="", help="<Optional> Extra boot commandline parameter to pass.")
    parser.add_argument("-a", "--artifact-path", dest="artifact_path", default=os.getcwd(), help="<Optional> Path to generate iso in.")

    options = parser.parse_args()

    if options.function == "build-iso" and not options.custom_packages_json:
        raise Exception("Custom packages json not provided...")
    elif options.function == "build-rpm-ostree-iso":
        if not options.ostree_tar_path:
            raise Exception("Ostree tar path not provided...")
        if not options.custom_packages_json:
            options.custom_packages_json = f"{os.path.dirname(__file__)}/packages_ostree_host.json"

    isoBuilder = IsoBuilder(function=options.function, custom_packages_json=options.custom_packages_json,
                            kickstart_path=options.kickstart_path, photon_release_version=options.photon_release_version,
                            log_level=options.log_level, initrd_pkg_list_file=options.custom_initrd_pkgs, ostree_tar_path=options.ostree_tar_path,
                            additional_repos=options.additional_repos, boot_cmdline_param=options.boot_cmdline_param, artifact_path=options.artifact_path)
    if options.function in ["build-iso", "build-rpm-ostree-iso"]:
        isoBuilder.logger.info(f"Starting to generate photon {isoBuilder.photon_release_version} iso...")
        isoBuilder.build()
    elif options.function == "build-initrd":
        isoBuilder.logger.info(f"Starting to generate photon {isoBuilder.photon_release_version} initrd.img...")
        isoBuilder.generateInitrd()
        # Move initrd image to current directory before cleaning up.
        isoBuilder.logger.debug(f"Moving {isoBuilder.working_dir}/initrd.img to {options.artifact_path}")
        shutil.move(f"{isoBuilder.working_dir}/initrd.img", options.artifact_path)
    else:
        raise Exception(f"{options.function} not supported...")

    isoBuilder.cleanUp()

if __name__ == '__main__':
    main()
