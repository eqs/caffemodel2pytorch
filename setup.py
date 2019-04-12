# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

setup(
    name='caffemodel2pytorch', 
    description='Convert caffemodels to pytorch model', 

    version='0.1', 
    author='Satoshi Murashige', 
    author_email='murashige.satoshi.mi1 [at] is.naist.jp', 
    url='https://github.com/eqs/caffemodel2pytorch', 
    
    entry_points={
        'console_scripts': ['caffemodel2pytorch=caffemodel2pytorch.caffemodel2pytorch:main']
    }, 

    packages=find_packages()
)

