#!/usr/bin/env python
import pymysql
pymysql.install_as_MySQLdb()

import os
import sys

def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecom_backend.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise
    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()
