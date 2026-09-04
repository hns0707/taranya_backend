from django.db import migrations


def forwards_backfill_stone_fks(apps, schema_editor):
    StoneVariant = apps.get_model("shared", "StoneVariant")
    ProductBOM = apps.get_model("shared", "ProductBOM")
    PurchaseOrderLot = apps.get_model("shared", "PurchaseOrderLot")

    for bom in ProductBOM.objects.filter(material_type="STONE").exclude(stone_variant_id=None).filter(
        stone_id__isnull=True
    ):
        sv = StoneVariant.objects.filter(pk=bom.stone_variant_id).first()
        if sv:
            ProductBOM.objects.filter(pk=bom.pk).update(stone_id=sv.stone_id)

    for lot in PurchaseOrderLot.objects.exclude(stone_variant_id=None).filter(stone_id__isnull=True):
        sv = StoneVariant.objects.filter(pk=lot.stone_variant_id).first()
        if sv:
            PurchaseOrderLot.objects.filter(pk=lot.pk).update(stone_id=sv.stone_id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("shared", "0003_stone_expand_fields"),
    ]

    operations = [
        migrations.RunPython(forwards_backfill_stone_fks, noop_reverse),
        migrations.RemoveField(
            model_name="productbom",
            name="stone_variant",
        ),
        migrations.RemoveField(
            model_name="productstone",
            name="variant",
        ),
        migrations.RemoveField(
            model_name="purchaseorderlot",
            name="stone_variant",
        ),
        migrations.DeleteModel(
            name="StoneVariant",
        ),
    ]
