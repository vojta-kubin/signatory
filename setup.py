
import setuptools
import sys



setuptools.setup(name='dummy_signatory',
                 version='0.1.0',
                 install_requires=[
                     'torch==1.11.0',
                    ],
                 package_dir = {'signatory':'./signatory'},
    )


