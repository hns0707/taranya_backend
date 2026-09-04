from django.db import migrations, models
import django.db.models.deletion


def forwards_backfill_stone_hsn(apps, schema_editor):
    Stone = apps.get_model("shared", "Stone")
    HSNMaster = apps.get_model("shared", "HSNMaster")
    by_code = {}
    for h in HSNMaster.objects.all().only("id", "hsn_code"):
        key = str(h.hsn_code).strip().upper()
        if key:
            by_code[key] = h.id
    for s in Stone.objects.exclude(hsn_code__isnull=True).exclude(hsn_code="").iterator(chunk_size=500):
        key = str(s.hsn_code).strip().upper()
        hid = by_code.get(key)
        if hid:
            Stone.objects.filter(pk=s.pk).update(hsn_id=hid)


def reverse_copy_hsn_code(apps, schema_editor):
    Stone = apps.get_model("shared", "Stone")
    HSNMaster = apps.get_model("shared", "HSNMaster")
    for s in Stone.objects.exclude(hsn_id__isnull=True).iterator(chunk_size=500):
        try:
            h = HSNMaster.objects.get(pk=s.hsn_id)
            Stone.objects.filter(pk=s.pk).update(hsn_code=h.hsn_code)
        except HSNMaster.DoesNotExist:
            pass


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0005_stone_code_name_expand"),
    ]

    operations = [
        migrations.AddField(
            model_name="stone",
            name="hsn",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional HSN master row for GST / reporting.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="stones",
                to="shared.hsnmaster",
            ),
        ),
        migrations.RunPython(forwards_backfill_stone_hsn, reverse_copy_hsn_code),
        migrations.RemoveField(
            model_name="stone",
            name="hsn_code",
        ),
    ]
