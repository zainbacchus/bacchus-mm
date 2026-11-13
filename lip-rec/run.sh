#!/bin/sh
# One piece of the recorder: about 1 hour 50 minutes, then it exits and must be started again.
cd "$(dirname "$0")/.." || exit 1
export LIP_END=1794863940 LIP_CHUNK="${LIP_CHUNK:-6600}" LIP_ROTATE="${LIP_ROTATE:-900}"
exec python3 lip-rec/lip_cloud.py lip-rec
