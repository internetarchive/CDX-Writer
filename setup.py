# This is a minimal setup script intended just for building PEX.
# Generate cdx_writer.pex with:
#   pex -r requirements.txt -o cdx_writer.pex -m cdx_writer .
from setuptools import setup, find_packages

setup(
    name='CDX-Writer',
    version='0.3.1',
    py_modules=['cdx_writer'],
    packages=find_packages(),
    install_requires=[
        'python-dateutil'
        ]
    )
