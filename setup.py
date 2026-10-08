# -*- coding: utf-8 -*-
import re
from pathlib import Path

from setuptools import setup, find_packages


def _version():
    text = Path('termius/__init__.py').read_text(encoding='utf-8')
    match = re.search(r"^__version__ = '([^']+)'", text, re.M)
    if not match:
        raise RuntimeError('Cannot read version from termius/__init__.py')
    return match.group(1)


requires = [
    'requests>=2.7.0',
    'cryptography>=3.2',
    'six>=1.10.0',
    'cached-property>=1.3.0',
    'paramiko>=1.16.0',
    'pathlib2>=2.1.0',
    'blinker>=1.4',
    'pynacl>=1.5.0',
    'python-socketio>=5.11.0',
    'websocket-client>=1.6.0',
    'keyring>=23.0',
]


def get_long_description():
    with open('README.md', encoding='utf-8') as handle:
        return handle.read()


setup(
    name='termius-mcp',
    version=_version(),
    license='BSD',
    author='MiaM1ku',
    author_email='61079068+MiaM1ku@users.noreply.github.com',
    url='https://github.com/MiaM1ku/termius-mcp',
    project_urls={
        'Source': 'https://github.com/MiaM1ku/termius-mcp',
        'Issues': 'https://github.com/MiaM1ku/termius-mcp/issues',
    },
    description='Termius Cloud MCP server.',
    long_description=get_long_description(),
    long_description_content_type='text/markdown',
    keywords=['termius', 'mcp'],
    packages=find_packages(exclude=['tests']),
    install_requires=requires,
    python_requires='>=3.9',
    zip_safe=False,
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: Unix',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Utilities',
    ],
    entry_points={
        'console_scripts': [
            'termius = termius.main:main',
            'termius-mcp = termius.main:main',
        ],
    },
)
