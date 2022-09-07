#!/bin/sh

# REQUIREMENTS: pyenv with a Python 3.7 version installed (change version here)
PYTHON_VERSION=3.7.13
VENV_DIR_NAME=scamp-venv

pyenv local $PYTHON_VERSION
python -m venv $VENV_DIR_NAME
source $VENV_DIR_NAME/bin/activate

# This is a workaround for a problem in googlemaps setup
REQUESTS_VERSION=`grep "requests==" requirements-frozen.txt`
python -m pip install $REQUESTS_VERSION

python -m pip install -r requirements-frozen.txt

echo ""
echo ==========================================
echo Ready to run scamp
echo $PWD/$VENV_DIR_NAME/bin/python $PWD/main.py --help
echo "Don't forget to install chromedriver: https://chromedriver.chromium.org/downloads"
