from importlib.metadata import MetadataPathFinder
from rest_framework.serializers import ModelSerializer,  ValidationError
from organization.models import Branch, Organization


class OrganizationSerializer(ModelSerializer):
    class Meta:
        model = Organization
        fields = '__all__'

    def validate(self, attrs):
        # self.instance is None only on create
        if self.instance is None and Organization.objects.active().exists():
            raise ValidationError("Only one organization is allowed.")
        return attrs


class BranchSerializer(ModelSerializer):
    class Meta:
        model = Branch
        # fields = [
        #     "id",
        #     "name",
        #     "address",
        #     "contact_number",
        #     "branch_manager",
        #     "organization",
        #     "branch_code",
        # ]
        fields = "__all__"  # keep whatever you already have

    # def validate(self, attrs):
    #     if self.instance is None and Branch.objects.active().exists():
    #         raise serializers.ValidationError(
    #             "Only one branch is allowed. A branch already exists."
    #         )
    #     return attrs
        
from rest_framework import serializers
from organization.models import PrinterSetting
class PrinterSettingSerializer(ModelSerializer):
    
    type = serializers.SerializerMethodField()

    class Meta:
        model = PrinterSetting
        fields = "url", "port", "ip", "type", "print_status"
    
    def get_type(self, obj):
        return obj.printer_location
