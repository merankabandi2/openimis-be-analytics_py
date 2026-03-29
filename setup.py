from setuptools import setup, find_packages

setup(
    name='openimis-be-analytics',
    version='1.0.0',
    packages=find_packages(include=['analytics']),
    include_package_data=True,
    license='AGPL-3.0',
    description='OpenIMIS Backend Analytics Module - Self-service analytics for data exploration',
    author='OpenIMIS',
    author_email='info@openimis.org',
    url='https://github.com/openimis/openimis-be-analytics_py',
    install_requires=[
        'django',
        'djangorestframework',
        'graphene-django',
        'openimis-be-core',
        'pandas',
        'openpyxl',
    ],
    classifiers=[
        'Environment :: Web Environment',
        'Framework :: Django',
        'Framework :: Django :: 3.0',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
    ],
)