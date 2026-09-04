from django.db import connection, migrations, models
import django.db.models.deletion


def _table_exists(table_name: str) -> bool:
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
    return table_name in tables


def create_communication_log_if_needed(apps, schema_editor):
    if _table_exists("crm_communication_logs"):
        return
    CommunicationLog = apps.get_model("shared", "CommunicationLog")
    # Model state exists; create table via schema editor
    schema_editor.create_model(CommunicationLog)


def create_scheduled_reminder_if_needed(apps, schema_editor):
    if _table_exists("crm_scheduled_reminders"):
        return
    CrmScheduledReminder = apps.get_model("shared", "CrmScheduledReminder")
    schema_editor.create_model(CrmScheduledReminder)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    """
    Creates CommunicationLog + CrmScheduledReminder.
    Safe if crm_communication_logs was created manually earlier.
    """

    dependencies = [
        ("shared", "0028_crm_prospect_contact"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="CommunicationLog",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        (
                            "channel",
                            models.CharField(
                                choices=[("WHATSAPP", "WhatsApp"), ("SMS", "SMS"), ("CALL", "Call")],
                                db_index=True,
                                max_length=16,
                            ),
                        ),
                        (
                            "message_type",
                            models.CharField(
                                choices=[
                                    ("SCHEME_REMINDER", "Scheme Payment Reminder"),
                                    ("UDHAR_REMINDER", "Udhar/Booking Reminder"),
                                    ("INVOICE", "Invoice"),
                                    ("OFFER", "Offer / Promotion"),
                                    ("CUSTOM", "Custom"),
                                ],
                                db_index=True,
                                max_length=32,
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[("SENT", "Sent"), ("FAILED", "Failed"), ("SKIPPED", "Skipped")],
                                db_index=True,
                                default="SENT",
                                max_length=16,
                            ),
                        ),
                        ("phone", models.CharField(max_length=20)),
                        ("template_name", models.CharField(blank=True, default="", max_length=128)),
                        ("parameters", models.TextField(blank=True, default="")),
                        ("message_body", models.TextField(blank=True, default="")),
                        ("ref_invoice_id", models.IntegerField(blank=True, db_index=True, null=True)),
                        ("ref_instalment_id", models.IntegerField(blank=True, db_index=True, null=True)),
                        ("api_response", models.TextField(blank=True, default="")),
                        ("error_detail", models.TextField(blank=True, default="")),
                        ("campaign_name", models.CharField(blank=True, db_index=True, default="", max_length=128)),
                        ("sent_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                        (
                            "customer",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="communication_logs",
                                to="shared.customer",
                            ),
                        ),
                        (
                            "sent_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="sent_communication_logs",
                                to="shared.adminuser",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "crm_communication_logs",
                        "ordering": ["-sent_at"],
                    },
                ),
                migrations.CreateModel(
                    name="CrmScheduledReminder",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("system_created_at", models.DateTimeField(auto_now_add=True)),
                        ("system_updated_at", models.DateTimeField(auto_now=True)),
                        ("phone", models.CharField(max_length=20)),
                        (
                            "channel",
                            models.CharField(
                                choices=[("WHATSAPP", "WhatsApp"), ("SMS", "SMS"), ("CALL", "Call")],
                                db_index=True,
                                max_length=16,
                            ),
                        ),
                        (
                            "message_type",
                            models.CharField(
                                choices=[
                                    ("SCHEME_REMINDER", "Scheme Payment Reminder"),
                                    ("UDHAR_REMINDER", "Udhar/Booking Reminder"),
                                    ("OFFER", "Offer / Promotion"),
                                    ("CUSTOM", "Custom"),
                                ],
                                db_index=True,
                                default="CUSTOM",
                                max_length=32,
                            ),
                        ),
                        ("scheduled_at", models.DateTimeField(db_index=True)),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("PENDING", "Pending"),
                                    ("SENT", "Sent"),
                                    ("FAILED", "Failed"),
                                    ("CANCELLED", "Cancelled"),
                                    ("SKIPPED", "Skipped"),
                                ],
                                db_index=True,
                                default="PENDING",
                                max_length=16,
                            ),
                        ),
                        ("template_name", models.CharField(blank=True, default="", max_length=128)),
                        ("parameters", models.TextField(blank=True, default="")),
                        ("message_body", models.TextField(blank=True, default="")),
                        ("campaign_name", models.CharField(blank=True, db_index=True, default="", max_length=128)),
                        ("ref_instalment_id", models.IntegerField(blank=True, db_index=True, null=True)),
                        ("ref_invoice_id", models.IntegerField(blank=True, db_index=True, null=True)),
                        ("notes", models.TextField(blank=True, default="")),
                        ("processed_at", models.DateTimeField(blank=True, null=True)),
                        ("error_detail", models.TextField(blank=True, default="")),
                        (
                            "communication_log",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="scheduled_reminders",
                                to="shared.communicationlog",
                            ),
                        ),
                        (
                            "created_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="created_crmscheduledreminder_set",
                                to="shared.adminuser",
                            ),
                        ),
                        (
                            "customer",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="scheduled_reminders",
                                to="shared.customer",
                            ),
                        ),
                        (
                            "updated_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="updated_crmscheduledreminder_set",
                                to="shared.adminuser",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "crm_scheduled_reminders",
                        "ordering": ["scheduled_at"],
                    },
                ),
                migrations.AddIndex(
                    model_name="crmscheduledreminder",
                    index=models.Index(fields=["status", "scheduled_at"], name="crm_schedul_status_7a1b2c_idx"),
                ),
                migrations.AddIndex(
                    model_name="crmscheduledreminder",
                    index=models.Index(fields=["channel", "status"], name="crm_schedul_channel_3d4e5f_idx"),
                ),
            ],
            database_operations=[
                migrations.RunPython(create_communication_log_if_needed, noop_reverse),
                migrations.RunPython(create_scheduled_reminder_if_needed, noop_reverse),
            ],
        ),
    ]
