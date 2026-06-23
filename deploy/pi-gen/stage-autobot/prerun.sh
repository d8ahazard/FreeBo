#!/bin/bash -e
# Standard pi-gen stage prerun: start this stage's rootfs from the previous stage's output.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
