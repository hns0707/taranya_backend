"""
Shared helper functions for the eCommerce Jewellery Savings Platform.
"""


def get_payment_mode_display(payment):
    """
    Return payment mode code for display. Supports PaymentCollection (split/single) and legacy payment_mode.
    For split payments returns 'SPLIT'; for single returns the mode code.
    """
    collections = list(payment.collections.select_related('payment_mode').all()[:10])
    if collections:
        return 'SPLIT' if len(collections) > 1 else (collections[0].payment_mode.code if collections[0].payment_mode_id else None)
    return payment.payment_mode.code if getattr(payment, 'payment_mode_id', None) else None


def get_payment_mode_label(payment):
    """Return payment mode label for display. For split returns 'Split Payment'."""
    mode_code = get_payment_mode_display(payment)
    if mode_code == 'SPLIT':
        return 'Split Payment'
    collections = list(payment.collections.select_related('payment_mode').all()[:1])
    if collections and collections[0].payment_mode_id:
        return getattr(collections[0].payment_mode, 'label', None) or mode_code
    if payment.payment_mode_id and hasattr(payment, 'payment_mode') and payment.payment_mode:
        return getattr(payment.payment_mode, 'label', None) or mode_code
    return mode_code or ''


def format_currency(amount: float, currency: str = "INR") -> str:
    """
    Format a currency amount with the specified currency symbol.
    
    Args:
        amount (float): The amount to format.
        currency (str): The currency symbol (default: "INR").
    
    Returns:
        str: Formatted currency string.
    """
    return f"{currency} {amount:,.2f}"

def calculate_discounted_price(original_price: float, discount_percentage: float) -> float:
    """
    Calculate the discounted price based on the original price and discount percentage.
    
    Args:
        original_price (float): The original price.
        discount_percentage (float): The discount percentage (e.g., 10 for 10%).
    
    Returns:
        float: The discounted price.
    """
    discount_amount = original_price * (discount_percentage / 100)
    return original_price - discount_amount

def build_user_permission_tree(user):
    """
    Build a hierarchical permission tree for a user based on their assigned roles.
    Super admins receive the full active permission tree (same as API authorization).
    
    Args:
        user (AdminUser): The admin user object.
    
    Returns:
        list: Hierarchical permission tree.
    """
    # Import here to avoid "populate() isn't reentrant" when helper is loaded during Django setup
    from shared.models import AdminUserRole, RolePermission, Permission

    if getattr(user, "is_super_admin", False):
        permissions = (
            Permission.objects.filter(is_active=True)
            .select_related("section", "section__module", "section__sub_module", "action")
        )
        return _build_permission_tree_from_permissions(permissions)

    # Get all roles assigned to the user
    user_roles = AdminUserRole.objects.filter(admin_user=user).select_related("role")
    role_ids = [user_role.role.id for user_role in user_roles]

    if not role_ids:
        return []

    # Get all permissions assigned to the user's roles
    role_permissions = RolePermission.objects.filter(role_id__in=role_ids).select_related("permission")
    permission_ids = [rp.permission.id for rp in role_permissions]
    permissions = Permission.objects.filter(id__in=permission_ids).select_related(
        "section", "section__module", "section__sub_module", "action"
    )

    return _build_permission_tree_from_permissions(permissions)


def _build_permission_tree_from_permissions(permissions):
    """Build module → submodule → section hierarchy from a permission queryset."""
    modules = {}

    for perm in permissions:
        section = perm.section
        module = section.module
        sub_module = section.sub_module
        
        # Create module entry if not exists
        if module.id not in modules:
            modules[module.id] = {
                "node_type": "MODULE",
                "id": module.id,
                "name": module.name,
                "code": module.code,
                "children": []
            }
        
        module_entry = modules[module.id]
        
        if sub_module:
            # Handle submodules (supports unlimited nesting)
            submodule_hierarchy = _get_submodule_hierarchy(sub_module)
            # Find or create submodule path
            current_level = module_entry["children"]
            for submodule in submodule_hierarchy:
                submodule_entry = next((child for child in current_level if child["id"] == submodule["id"]), None)
                if not submodule_entry:
                    submodule_entry = {
                        "node_type": "SUBMODULE",
                        "id": submodule["id"],
                        "name": submodule["name"],
                        "code": submodule["code"],
                        "children": []
                    }
                    current_level.append(submodule_entry)
                current_level = submodule_entry["children"]
            # Add section to the deepest submodule
            section_entry = next((child for child in current_level if child["id"] == section.id), None)
            if not section_entry:
                section_entry = {
                    "node_type": "SECTION",
                    "id": section.id,
                    "name": section.name,
                    "code": section.code,
                    "actions": []
                }
                current_level.append(section_entry)
        else:
            # Add section directly to module
            section_entry = next((child for child in module_entry["children"] if child["id"] == section.id), None)
            if not section_entry:
                section_entry = {
                    "node_type": "SECTION",
                    "id": section.id,
                    "name": section.name,
                    "code": section.code,
                    "actions": []
                }
                module_entry["children"].append(section_entry)
        
        # Add action to section
        if perm.action.code not in section_entry["actions"]:
            section_entry["actions"].append(perm.action.code)
    
    # Sort all levels
    for module in modules.values():
        # Sort module children
        module["children"].sort(key=lambda x: x["name"])
        for child in module["children"]:
            if child["node_type"] == "SUBMODULE":
                # Sort submodule children
                child["children"].sort(key=lambda x: x["name"])
                for subchild in child["children"]:
                    if subchild["node_type"] == "SUBMODULE":
                        subchild["children"].sort(key=lambda x: x["name"])
                    else:
                        subchild["actions"].sort()
            else:
                child["actions"].sort()
    
    # Convert modules dict to list
    modules_list = list(modules.values())
    modules_list.sort(key=lambda x: x["name"])

    return modules_list


def _get_submodule_hierarchy(sub_module):
    """Get the hierarchy of a submodule from root to current submodule."""
    hierarchy = []
    current = sub_module
    while current:
        hierarchy.insert(0, {
            "id": current.id,
            "name": current.name,
            "code": current.code
        })
        current = current.parent
    return hierarchy