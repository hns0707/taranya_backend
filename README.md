# eCommerce Jewellery Savings Platform - Django Backend

This is a Django-based backend for an eCommerce Jewellery Savings Platform. It includes two main apps: `master` for admin/backoffice functionalities and `customer` for customer-facing APIs.

## Project Structure

```
ecom_backend/
├── ecom_backend/
│   ├── settings.py
│   ├── urls.py
│   └── ...
├── master/
│   ├── permissions/
│   │   ├── __init__.py
│   │   └── permission_checker.py
│   ├── views/
│   │   ├── __init__.py
│   │   ├── auth_view.py
│   │   ├── scheme_view.py
│   │   ├── gold_view.py
│   │   ├── cms_view.py
│   │   ├── faq_view.py
│   │   └── admin_user_view.py
│   ├── urls.py
│   └── ...
├── customer/
│   ├── views/
│   │   ├── __init__.py
│   │   ├── auth_view.py
│   │   ├── profile_view.py
│   │   ├── scheme_view.py
│   │   ├── payment_view.py
│   │   └── redemption_view.py
│   ├── urls.py
│   └── ...
├── shared/
│   ├── __init__.py
│   ├── helper.py
│   ├── utility.py
│   ├── models.py
│   └── apps.py
├── manage.py
└── README.md
```

## Setup Instructions

### 1. Clone the Repository

```bash
git clone <repository-url>
cd ecom_backend
```

### 2. Create and Activate Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install django djangorestframework djangorestframework-simplejwt
```

### 4. Apply Migrations

```bash
python manage.py migrate
```

### 5. Run the Development Server

```bash
python manage.py runserver
```

## Features

### Admin Authentication & Authorization

- **JWT-based authentication** for admin users.
- **Decorator-based authorization** for securing admin APIs.
- **Role-based permissions** stored in the database.

### Admin APIs

- **Admin Login**: `/api/admin/auth/login/`
- **Admin Profile**: `/api/admin/auth/profile/`
- **Admin User CRUD**: `/api/admin/users/`
- **Role CRUD**: `/api/admin/roles/`
- **Permission CRUD**: `/api/admin/permissions/`
- **Role-Permission Assignment**: `/api/admin/roles/{role_id}/permissions/`

### Customer APIs

- **OTP Login**: `/api/customer/login/`
- **Customer Profile**: `/api/customer/profile/`
- **Scheme Enrollment**: `/api/customer/schemes/`
- **Payments**: `/api/customer/payments/`
- **Redemption**: `/api/customer/redemptions/`

### Shared Utilities

- **Helper Functions**: Currency formatting, calculations.
- **Utility Functions**: Email/phone validation, timestamp generation.
- **Shared Models**: Centralized models for both apps.

## Usage

### Admin Authentication

To secure an admin API, use the decorators:

```python
from master.permissions.permission_checker import admin_auth

@admin_auth("gold_rate.edit")
def update_gold_rate(request):
    # Your logic here
    pass
```

### Running Tests

```bash
python manage.py test
```

### Creating Superuser

```bash
python manage.py createsuperuser
```

## Contributing

1. Fork the repository.
2. Create a new branch.
3. Make your changes.
4. Submit a pull request.

## License

This project is licensed under the MIT License.