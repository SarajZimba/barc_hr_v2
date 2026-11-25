from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Sum, Q
from decimal import Decimal
from datetime import date
import json
import nepali_datetime

from employee.models import (
    Employee, Pay_Package, MasterPayPackage,
    tblleaveapply, tblEmployeeCommision,
    MasterDeduction, EmployeeDeduction
)
from organization.models import Organization
from salary.utils import calculate_tax, calculate_net_salary, get_nepali_date, calculate_tax_yearly


def get_monthly_equivalent(pay_package):
    """
    Returns this pay package's amount normalized to a monthly figure.
    If package_type is 'Yearly', the stored amount is annual and gets divided by 12.
    Everything else is assumed to already be a monthly figure.
    """
    amount = pay_package.amount if pay_package.amount else Decimal("0.0")
    package_type = pay_package.package.package_type if pay_package.package else None
    if package_type and package_type.strip().lower() == "yearly":
        return (amount / Decimal("12")).quantize(Decimal("0.01"))
    return amount

from datetime import date


def has_completed_one_year(hiring_date, as_of=None):
    """True if at least one full year has passed since hiring_date."""
    if not hiring_date:
        return False
    as_of = as_of or date.today()
    try:
        anniversary = hiring_date.replace(year=hiring_date.year + 1)
    except ValueError:
        # Hired on Feb 29: the anniversary falls on Mar 1 in a non-leap year
        anniversary = date(hiring_date.year + 1, 3, 1)
    return anniversary <= as_of


from datetime import timedelta
from decimal import Decimal

# Build month names from the library so spellings always match what strftime("%B") gives you
NEPALI_MONTHS = [nepali_datetime.date(2080, m, 1).strftime("%B") for m in range(1, 13)]
MONTH_INDEX = {name.lower(): i for i, name in enumerate(NEPALI_MONTHS)}

def nepali_month_range(nepali_year, nepali_month):
    """First and last English dates of a Nepali month."""
    m = NEPALI_MONTHS.index(nepali_month) + 1
    year = int(nepali_year)
    start = nepali_datetime.date(year, m, 1).to_datetime_date()
    if m == 12:
        next_start = nepali_datetime.date(year + 1, 1, 1).to_datetime_date()
    else:
        next_start = nepali_datetime.date(year, m + 1, 1).to_datetime_date()
    return start, next_start - timedelta(days=1)


def approved_leave_days_in_month(employee, nepali_year, nepali_month):
    start, end = nepali_month_range(nepali_year, nepali_month)
    leaves = tblleaveapply.objects.filter(
        empID=employee,
        current_status="Approved",
        leaveDateFrom__lte=end,
        leaveDateTo__gte=start,
    )
    total = Decimal("0")
    for leave in leaves:
        first = max(leave.leaveDateFrom, start)
        last = min(leave.leaveDateTo, end)
        total += Decimal((last - first).days + 1)
    return total


def get_leave_summary(employee, nepali_year, nepali_month):
    org = Organization.objects.first()
    allowance = org.noOfpaidleavesallowed if org else Decimal("0")
    used = approved_leave_days_in_month(employee, nepali_year, nepali_month)
    paid = min(used, allowance)
    return {
        "allowance": allowance,
        "used_days": used,
        "paid_days": paid,
        "unpaid_days": used - paid,
        "remaining": allowance - paid,
    }

def build_employee_salary_sheet(employee_obj, nepali_year, nepali_month):
    """
    Builds the salary sheet dict for a single employee for the given nepali_year/nepali_month.
    Shared by both the single-employee and branch-wide salary sheet endpoints.
    """
    branch = employee_obj.branch
    organization = branch.organization if branch else None
    dashain_month = organization.dashain_month if organization else None
    fiscal_year = branch.organization.current_fiscal_year if organization else None

    # ---------- 1. Gross salary / pay packages — normalized to monthly ----------
    pay_packages = Pay_Package.objects.filter(empID=employee_obj)

    monthly_amounts = {pp.id: get_monthly_equivalent(pp) for pp in pay_packages}
    total_amount = sum(monthly_amounts.values(), Decimal("0.0"))
    packagetotal_sum = total_amount  # used later as the "gross_salary" deduction base

    employee_package_dict = {}
    for pay_package in pay_packages:
        if not pay_package.package:
            continue
        package_name = pay_package.package.package_name
        employee_package_dict[package_name] = {
            "package_name": package_name,
            "amount": monthly_amounts[pay_package.id],
            "package_type": pay_package.package.package_type,
            "taxable": pay_package.package.taxable,
        }

    # master_packages = MasterPayPackage.objects.filter(
    #     Q(branch_id=employee_obj.branch_id) | Q(branch__isnull=True)
    # )
    master_packages = MasterPayPackage.objects.all()
    for master_package in master_packages:
        if master_package.package_name not in employee_package_dict:
            employee_package_dict[master_package.package_name] = {
                "package_name": master_package.package_name,
                "amount": Decimal("0.0"),
                "package_type": master_package.package_type,
                "taxable": master_package.taxable,
            }

    def get_basic_salary_monthly():
        pp = pay_packages.filter(package__package_name="Basic Salary").first()
        return monthly_amounts.get(pp.id, Decimal("0.0")) if pp else Decimal("0.0")

    # FIX: track one-off additions (Dashain bonus, commissions) that are paid on top
    # of salary and must be added to the net amount.
    extra_keys = set()

    if dashain_month and nepali_month == dashain_month:
        eligible = True
        if branch.organization.dashain_allowanceafteroneyear_workcompletion:
            eligible = has_completed_one_year(employee_obj.hiring_date)

        if eligible:
            basic_salary_monthly = get_basic_salary_monthly()
            if basic_salary_monthly:
                employee_package_dict["Dashain Bonus"] = {
                    "package_name": "Dashain Bonus",
                    "amount": basic_salary_monthly,
                    "package_type": "Bonus",
                    "taxable": False,
                }
                extra_keys.add("Dashain Bonus")  # FIX

    commissions = tblEmployeeCommision.objects.filter(empID=employee_obj, month=nepali_month)
    for commission in commissions:
        employee_package_dict[commission.type] = {
            "package_name": commission.type,
            "amount": commission.amount,
            "package_type": "Bonus",
            "taxable": False,
        }
        extra_keys.add(commission.type)  # FIX (remove this line if commissions are paid outside payroll)

    # FIX: total of the non-taxable additions, added to the net at the end
    non_taxable_additions = sum(
        (Decimal(str(employee_package_dict[k]["amount"])) for k in extra_keys),
        Decimal("0.0"),
    )

    employee_package_list = sorted(employee_package_dict.values(), key=lambda x: x["package_name"])

    # ---------- 2. Leave deductions ----------
    leaves = tblleaveapply.objects.filter(
        Q(nepali_year=nepali_year) &
        Q(nepali_month__icontains=f'"{nepali_month}"') &
        Q(current_status="Approved") &
        Q(empID=employee_obj)
    )

    # ---------- 2. Leave deductions ----------
    leave_summary = get_leave_summary(employee_obj, nepali_year, nepali_month)
    paid_leaves_sum = leave_summary["paid_days"]
    unpaid_leaves_sum = leave_summary["unpaid_days"]

    unpaid_leavesdeduction = round(float(unpaid_leaves_sum) * float(total_amount / 30), 2)
    total_amount -= Decimal(str(unpaid_leavesdeduction))

    # ---------- 3. Deductions (MasterDeduction / EmployeeDeduction) ----------
    running_total = total_amount

    # active_deductions = MasterDeduction.objects.filter(used=True).filter(
    #     Q(branch_id=employee_obj.branch_id) | Q(branch__isnull=True)
    # ).order_by("order")
    active_deductions = MasterDeduction.objects.filter(used=True).order_by("order")

    overrides = {
        ed.deduction_id: ed.value
        for ed in EmployeeDeduction.objects.filter(empID=employee_obj)
    }

    deduction_list = []
    sst_waived = False

    for master_deduction in active_deductions:
        value = overrides.get(master_deduction.id)
        if value is None:
            value = master_deduction.default_value

        if master_deduction.based_on == "basic_salary":
            base_amount = get_basic_salary_monthly()
            if unpaid_leaves_sum != 0.0 and base_amount:
                base_amount = base_amount - (base_amount / 30) * Decimal(str(unpaid_leaves_sum))
        elif master_deduction.based_on == "gross_salary":
            base_amount = packagetotal_sum
        else:  # running_total
            base_amount = running_total

        if master_deduction.calculation_type == "percentage":
            deduction_amount = round(base_amount * (Decimal(str(value)) / Decimal("100")), 2)
        else:
            deduction_amount = Decimal(str(value))

        running_total -= deduction_amount

        deduction_list.append({
            "name": master_deduction.name,
            "amount": deduction_amount,
        })

        if master_deduction.waives_sst:
            sst_waived = True

    deduction_tax = calculate_tax(running_total, "sst_deduct_false" if sst_waived else "sst_deduct_true")
    total_amount = running_total

    # ---------- 4. Net salary ----------
    # FIX: add the non-taxable additions after tax
    net_salary = Decimal(str(calculate_net_salary(total_amount, deduction_tax))) + non_taxable_additions

    return {
        "name": employee_obj.name,
        "empID": employee_obj.id,
        "department": employee_obj.department,

        "total_gross_salary": {
            "paypackages": employee_package_list,
            "total_salary": total_amount,
        },
        "leave_deductions": {
            "unpaid_leavesdeduction": unpaid_leavesdeduction,
            "unpaid_leaves": unpaid_leaves_sum,
        },
        "fund_deductions": deduction_list,
        "tax_deduction": deduction_tax,
        "net_amount": net_salary,
        "month": nepali_month,
        "nepali_date": get_nepali_date()[1],
        "fiscal_year": fiscal_year,
    }


class EmployeeSalarySheetAPIView(APIView):
    """
    Returns the salary sheet for ONE employee for a given nepali month.
    Query params: empID (required), month (nepali month name, optional, defaults to current),
                  year (nepali year, optional, defaults to current),
                  branchID (optional, if sent the employee must belong to that branch)
    """
    def get(self, request, *args, **kwargs):
        emp_id = request.query_params.get("empID")
        if not emp_id:
            return Response({"error": "empID is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            emp_id = int(emp_id)
        except (ValueError, TypeError):
            return Response({"error": "Invalid empID."}, status=status.HTTP_400_BAD_REQUEST)

        employees = Employee.objects.filter(id=emp_id)

        branch_id = request.query_params.get("branchID")
        if branch_id:
            try:
                employees = employees.filter(branch_id=int(branch_id))
            except (ValueError, TypeError):
                return Response({"error": "Invalid branchID."}, status=status.HTTP_400_BAD_REQUEST)

        employee_obj = employees.first()
        if employee_obj is None:
            return Response({"error": "Employee not found."}, status=status.HTTP_404_NOT_FOUND)

        nepali_today = nepali_datetime.date.from_datetime_date(date.today())
        nepali_month = request.query_params.get("month") or nepali_today.strftime("%B")
        nepali_year = request.query_params.get("year")
        try:
            nepali_year = int(nepali_year) if nepali_year else nepali_today.year
        except (ValueError, TypeError):
            return Response({"error": "Invalid year."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            sheet = build_employee_salary_sheet(employee_obj, nepali_year, nepali_month)
        except Exception as e:
            return Response(
                {"empID": employee_obj.id, "name": employee_obj.name, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "month": nepali_month,
                "year": nepali_year,
                "salary_sheet": sheet,
            },
            status=status.HTTP_200_OK,
        )

# class BranchSalarySheetAPIView(APIView):
#     """
#     Returns salary sheets for all employees of a given branch, for a given nepali month.
#     Query params: branchID (required), month (nepali month name, optional — defaults to current),
#                   year (nepali year, optional — defaults to current)
#     """
#     def get(self, request, *args, **kwargs):
#         branch_id = request.query_params.get("branchID")
#         if not branch_id:
#             return Response({"error": "branchID is required."}, status=status.HTTP_400_BAD_REQUEST)

#         try:
#             branch_id = int(branch_id)
#         except (ValueError, TypeError):
#             return Response({"error": "Invalid branchID."}, status=status.HTTP_400_BAD_REQUEST)

#         current_date = date.today()
#         nepali_today = nepali_datetime.date.from_datetime_date(current_date)

#         nepali_month = request.query_params.get("month") or nepali_today.strftime("%B")
#         nepali_year = request.query_params.get("year")
#         try:
#             nepali_year = int(nepali_year) if nepali_year else nepali_today.year
#         except (ValueError, TypeError):
#             return Response({"error": "Invalid year."}, status=status.HTTP_400_BAD_REQUEST)

#         employees = Employee.objects.filter(branch_id=branch_id, current_status="Enrolled")

#         if not employees.exists():
#             return Response({"error": "No active employees found for this branch."}, status=status.HTTP_404_NOT_FOUND)

#         salary_sheets = []
#         for employee_obj in employees:
#             try:
#                 sheet = build_employee_salary_sheet(employee_obj, nepali_year, nepali_month)
#                 salary_sheets.append(sheet)
#             except Exception as e:
#                 # Don't let one bad employee record kill the whole branch report
#                 salary_sheets.append({
#                     "empID": employee_obj.id,
#                     "name": employee_obj.name,
#                     "error": str(e),
#                 })

#         return Response({
#             "branchID": branch_id,
#             "month": nepali_month,
#             "year": nepali_year,
#             "employee_count": employees.count(),
#             "salary_sheets": salary_sheets,
#         }, status=status.HTTP_200_OK)


from datetime import date

import nepali_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

# from <your_app>.models import Branch, Employee
# from <your_app>.utils import build_employee_salary_sheet


class BranchSalarySheetAPIView(APIView):
    """
    Returns salary sheets for all employees of the given branch(es), for a given nepali month.
    Query params:
      branchID (required) - one id, repeated ids (?branchID=1&branchID=2),
                            comma separated (?branchID=1,2,5) or "all"
      month    (optional) - nepali month name, defaults to current
      year     (optional) - nepali year, defaults to current
    """

    def get(self, request, *args, **kwargs):
        raw_values = request.query_params.getlist("branchID")
        if not raw_values:
            return Response({"error": "branchID is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Flatten repeated and comma-separated values
        tokens = [t.strip() for v in raw_values for t in v.split(",") if t.strip()]
        if not tokens:
            return Response({"error": "branchID is required."}, status=status.HTTP_400_BAD_REQUEST)

        nepali_today = nepali_datetime.date.from_datetime_date(date.today())
        nepali_month = request.query_params.get("month") or nepali_today.strftime("%B")
        nepali_year = request.query_params.get("year")
        try:
            nepali_year = int(nepali_year) if nepali_year else nepali_today.year
        except (ValueError, TypeError):
            return Response({"error": "Invalid year."}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve branches
        missing_ids = []
        if any(t.lower() == "all" for t in tokens):
            branches = Branch.objects.all().order_by("id")
        else:
            try:
                branch_ids = list(dict.fromkeys(int(t) for t in tokens))  # dedupe, keep order
            except ValueError:
                return Response({"error": "Invalid branchID."}, status=status.HTTP_400_BAD_REQUEST)

            branches = Branch.objects.filter(id__in=branch_ids).order_by("id")
            found = set(branches.values_list("id", flat=True))
            missing_ids = [b for b in branch_ids if b not in found]

        if not branches.exists():
            return Response({"error": "No branches found."}, status=status.HTTP_404_NOT_FOUND)

        branch_results = []
        total_employees = 0

        for branch in branches:
            employees = Employee.objects.filter(branch=branch, current_status="Enrolled")
            employee_count = employees.count()

            if employee_count == 0:
                branch_results.append({
                    "branchID": branch.id,
                    "branch_name": branch.name,
                    "employee_count": 0,
                    "salary_sheets": [],
                    "warning": "No active employees found for this branch.",
                })
                continue

            salary_sheets = []
            for employee_obj in employees:
                try:
                    salary_sheets.append(
                        build_employee_salary_sheet(employee_obj, nepali_year, nepali_month)
                    )
                except Exception as e:
                    # Don't let one bad employee record kill the whole report
                    salary_sheets.append({
                        "empID": employee_obj.id,
                        "name": employee_obj.name,
                        "error": str(e),
                    })

            total_employees += employee_count
            branch_results.append({
                "branchID": branch.id,
                "branch_name": branch.name,
                "employee_count": employee_count,
                "salary_sheets": salary_sheets,
            })

        response = {
            "month": nepali_month,
            "year": nepali_year,
            "branch_count": len(branch_results),
            "employee_count": total_employees,
            "branches": branch_results,
        }
        if missing_ids:
            response["missing_branchIDs"] = missing_ids

        return Response(response, status=status.HTTP_200_OK)

class EmployeeYearlySalarySheetAPIView(APIView):
    def get(self, request, *args, **kwargs):
        emp_id = request.query_params.get("empID")
        if not emp_id:
            return Response({"error": "empID is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            employee_obj = Employee.objects.get(id=int(emp_id))
        except (Employee.DoesNotExist, ValueError, TypeError):
            return Response({"error": "Invalid empID."}, status=status.HTTP_400_BAD_REQUEST)

        branch = employee_obj.branch
        organization = branch.organization if branch else None
        fiscal_year = organization.current_fiscal_year if organization else None

        # ---------- 1. Gross salary / pay packages, annualized ----------
        pay_packages = Pay_Package.objects.filter(empID=employee_obj)
        packagetotal_sum = pay_packages.aggregate(Sum("amount"))["amount__sum"]
        monthly_gross = packagetotal_sum if packagetotal_sum is not None else Decimal("0.0")

        employee_package_list = []
        for pay_package in pay_packages:
            if not pay_package.package:
                continue
            monthly_amount = pay_package.amount if pay_package.amount else Decimal("0.0")
            employee_package_list.append({
                "package_name": pay_package.package.package_name,
                "amount": monthly_amount * 12,
                "package_type": pay_package.package.package_type,
                "taxable": pay_package.package.taxable,
            })

        employee_package_list.sort(key=lambda x: x["package_name"])

        yearly_gross = monthly_gross * 12

        # ---------- 2. Deductions, annualized ----------
        running_total = yearly_gross

        active_deductions = MasterDeduction.objects.filter(used=True).filter(
            Q(branch_id=employee_obj.branch_id) | Q(branch__isnull=True)
        ).order_by("order")

        overrides = {
            ed.deduction_id: ed.value
            for ed in EmployeeDeduction.objects.filter(empID=employee_obj)
        }

        deduction_list = []
        sst_waived = False

        for master_deduction in active_deductions:
            value = overrides.get(master_deduction.id)
            if value is None:
                value = master_deduction.default_value

            if master_deduction.based_on == "basic_salary":
                basic_salary_package = pay_packages.filter(package__package_name="Basic Salary").first()
                base_amount = (basic_salary_package.amount * 12) if basic_salary_package else Decimal("0.0")
            elif master_deduction.based_on == "gross_salary":
                base_amount = yearly_gross
            else:  # running_total
                base_amount = running_total

            if master_deduction.calculation_type == "percentage":
                deduction_amount = round(base_amount * (Decimal(str(value)) / Decimal("100")), 2)
            else:
                deduction_amount = Decimal(str(value)) * 12  # flat deductions are per-month, so annualize

            running_total -= deduction_amount

            deduction_list.append({
                "name": master_deduction.name,
                "amount": deduction_amount,
            })

            if master_deduction.waives_sst:
                sst_waived = True

        # ---------- 3. Tax, computed directly on yearly figures ----------
        yearly_tax = calculate_tax_yearly(running_total, "sst_deduct_false" if sst_waived else "sst_deduct_true")
        total_tax = next(item["Total"] for item in yearly_tax if item["name"] == "Total Tax")

        net_yearly = running_total - Decimal(str(total_tax))

        response_data = {
            "name": employee_obj.name,
            "empID": employee_obj.id,
            "total_gross_salary": {
                "paypackages": employee_package_list,
                "total_salary": running_total,
            },
            "fund_deductions": deduction_list,
            "tax_deduction": yearly_tax,
            "net_amount": net_yearly,
            "fiscal_year": fiscal_year,
        }

        return Response(response_data, status=status.HTTP_200_OK)


from django.db import transaction

from organization.models import Branch
from employee.models import Employee
from salary.models import (
    MonthlySalaryReport,
    EmployeeSalary,
    EmployeeSalaryPayPackage,
    LeaveDeduction,
    FundDeduction,
    TaxDeduction,
)


@transaction.atomic
def save_monthly_salary_report(branch, nepali_year, nepali_month, employees):
    report, _ = MonthlySalaryReport.objects.update_or_create(
        branch=branch, nepali_year=nepali_year, month=nepali_month,
        defaults={
            "fiscal_year": branch.organization.current_fiscal_year if branch.organization else None,
            "nepali_date": get_nepali_date()[1],
        },
    )
    report.employee_salaries.all().delete()

    for emp in employees:
        sheet = build_employee_salary_sheet(emp, nepali_year, nepali_month)

        emp_salary = EmployeeSalary.objects.create(
            monthly_report=report,
            empID=emp,
            name=sheet["name"],
            gross_salary=sum(pp["amount"] for pp in sheet["total_gross_salary"]["paypackages"]),
            total_salary=sheet["total_gross_salary"]["total_salary"],
            net_amount=sheet["net_amount"],
        )

        EmployeeSalaryPayPackage.objects.bulk_create([
            EmployeeSalaryPayPackage(
                employee=emp_salary,
                package_name=pp["package_name"],
                amount=pp["amount"],
                package_type=pp["package_type"],
                taxable=pp["taxable"],
            )
            for pp in sheet["total_gross_salary"]["paypackages"]
        ])

        LeaveDeduction.objects.create(
            employee=emp_salary,
            unpaid_leavesdeduction=sheet["leave_deductions"]["unpaid_leavesdeduction"],
            unpaid_leaves=sheet["leave_deductions"]["unpaid_leaves"],
        )

        FundDeduction.objects.bulk_create([
            FundDeduction(employee=emp_salary, name=fd["name"], amount=fd["amount"])
            for fd in sheet["fund_deductions"]
        ])

        TaxDeduction.objects.bulk_create([
            TaxDeduction(employee=emp_salary, name=td["name"], total=td["Total"])
            for td in sheet["tax_deduction"]
        ])

    return report


# class SaveMonthlySalaryReportAPIView(APIView):
#     def post(self, request):
#         branch_id = request.data.get("branchID")
#         nepali_year = request.data.get("year")
#         nepali_month = request.data.get("month")

#         if not all([branch_id, nepali_year, nepali_month]):
#             return Response(
#                 {"error": "branchID, year, and month are required."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         try:
#             branch = Branch.objects.get(id=branch_id)
#         except Branch.DoesNotExist:
#             return Response({"error": "Branch not found."}, status=status.HTTP_404_NOT_FOUND)

#         employees = Employee.objects.filter(branch=branch)
#         employee_count = employees.count()
#         if employee_count == 0:
#             return Response({"error": "No employees found for this branch."}, status=status.HTTP_404_NOT_FOUND)

#         existing_report = MonthlySalaryReport.objects.filter(
#             branch=branch, nepali_year=nepali_year, month=nepali_month
#         ).exists()

#         try:
#             report = save_monthly_salary_report(branch, nepali_year, nepali_month, employees)
#         except Exception as e:
#             return Response(
#                 {"error": f"Failed to save salary report: {str(e)}"},
#                 status=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             )

#         return Response(
#             {
#                 "message": "Salary report updated successfully." if existing_report else "Salary report saved successfully.",
#                 "reportID": report.id,
#                 "branchID": branch.id,
#                 "month": report.month,
#                 "year": report.nepali_year,
#                 "employee_count": employee_count,
#             },
#             status=status.HTTP_200_OK if existing_report else status.HTTP_201_CREATED,
#         )

from django.db import transaction

class SaveMonthlySalaryReportAPIView(APIView):
    def post(self, request):
        branch_ids = request.data.get("branchID")
        nepali_year = request.data.get("year")
        nepali_month = request.data.get("month")

        if not all([branch_ids, nepali_year, nepali_month]):
            return Response(
                {"error": "branchID, year, and month are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve which branches to process
        if branch_ids == "all":
            branches = Branch.objects.all()
        else:
            if not isinstance(branch_ids, (list, tuple)):
                branch_ids = [branch_ids]  # keep single-id requests working
            branches = Branch.objects.filter(id__in=branch_ids)

            found_ids = set(branches.values_list("id", flat=True))
            missing = [b for b in branch_ids if b not in found_ids]
        if branch_ids == "all":
            missing = []

        if not branches.exists():
            return Response({"error": "No branches found."}, status=status.HTTP_404_NOT_FOUND)

        results = []
        for branch in branches:
            employees = Employee.objects.filter(branch=branch)
            employee_count = employees.count()

            if employee_count == 0:
                results.append({
                    "branchID": branch.id,
                    "branch_name": branch.name,
                    "status": "skipped",
                    "error": "No employees found for this branch.",
                })
                continue

            existing_report = MonthlySalaryReport.objects.filter(
                branch=branch, nepali_year=nepali_year, month=nepali_month
            ).exists()

            try:
                with transaction.atomic():
                    report = save_monthly_salary_report(
                        branch, nepali_year, nepali_month, employees
                    )
            except Exception as e:
                results.append({
                    "branchID": branch.id,
                    "branch_name": branch.name,
                    "status": "failed",
                    "error": f"Failed to save salary report: {str(e)}",
                })
                continue

            results.append({
                "branchID": branch.id,
                "branch_name": branch.name,
                "status": "updated" if existing_report else "created",
                "reportID": report.id,
                "month": report.month,
                "year": report.nepali_year,
                "employee_count": employee_count,
            })

        for b in missing:
            results.append({"branchID": b, "status": "failed", "error": "Branch not found."})

        succeeded = [r for r in results if r["status"] in ("created", "updated")]
        failed = [r for r in results if r["status"] in ("failed", "skipped")]

        if not succeeded:
            http_status = status.HTTP_400_BAD_REQUEST
        elif failed:
            http_status = status.HTTP_207_MULTI_STATUS
        else:
            http_status = status.HTTP_200_OK

        return Response(
            {
                "message": f"{len(succeeded)} of {len(results)} branch salary reports saved.",
                "results": results,
            },
            status=http_status,
        )



def serialize_employee_salary(emp_salary):
    emp = emp_salary.empID
    return {
        "empID": emp_salary.empID_id,
        "name": emp_salary.name,
        "department": emp.department if emp else None,
        "gross_salary": emp_salary.gross_salary,
        "total_salary": emp_salary.total_salary,
        "net_amount": emp_salary.net_amount,
        "pay_packages": [
            {
                "package_name": pp.package_name,
                "amount": pp.amount,
                "package_type": pp.package_type,
                "taxable": pp.taxable,
            }
            for pp in emp_salary.pay_packages.all()
        ],
        "leave_deductions": [
            {
                "unpaid_leavesdeduction": ld.unpaid_leavesdeduction,
                "unpaid_leaves": ld.unpaid_leaves,
            }
            for ld in emp_salary.leave_deductions.all()
        ],
        "fund_deductions": [
            {"name": fd.name, "amount": fd.amount}
            for fd in emp_salary.fund_deductions.all()
        ],
        "tax_deductions": [
            {"name": td.name, "total": td.total}
            for td in emp_salary.tax_deductions.all()
        ],
    }

from organization.utils import resolve_branches

def build_report_detail(branch, nepali_year, nepali_month):
    report = MonthlySalaryReport.objects.filter(
        branch=branch, nepali_year=nepali_year, month=nepali_month
    ).prefetch_related(
        "employee_salaries__pay_packages",
        "employee_salaries__leave_deductions",
        "employee_salaries__fund_deductions",
        "employee_salaries__tax_deductions",
    ).first()
    if not report:
        return None
    return {
        "reportID": report.id,
        "branchID": branch.id,
        "branch_name": branch.name,
        "month": report.month,
        "year": report.nepali_year,
        "nepali_date": report.nepali_date,
        "fiscal_year": report.fiscal_year,
        "employee_count": report.employee_salaries.count(),
        "salary_sheets": [serialize_employee_salary(e) for e in report.employee_salaries.all()],
    }


class MonthlySalaryReportDetailAPIView(APIView):
    """
    GET /branch/<branch_id>/salary-report/?year=2082&month=Shrawan           (one branch, old shape)
    GET /salary-report/?branchID=1,2,5|all&year=2082&month=Shrawan           (grouped by branch)
    """
    def get(self, request, branch_id=None):
        nepali_year = request.query_params.get("year")
        nepali_month = request.query_params.get("month")
        if not all([nepali_year, nepali_month]):
            return Response({"error": "year and month are required."}, status=status.HTTP_400_BAD_REQUEST)

        branches, missing, error = resolve_branches(request, branch_id)
        if error:
            return error

        # Old single-branch behaviour, unchanged
        if branch_id is not None:
            payload = build_report_detail(branches[0], nepali_year, nepali_month)
            if payload is None:
                return Response(
                    {"error": "No salary report found for this branch/year/month."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(payload, status=status.HTTP_200_OK)

        results = []
        for branch in branches:
            payload = build_report_detail(branch, nepali_year, nepali_month)
            if payload is None:
                results.append({
                    "branchID": branch.id,
                    "branch_name": branch.name,
                    "status": "not_found",
                    "error": "No salary report found for this branch/year/month.",
                })
            else:
                results.append({**payload, "status": "found"})

        response = {"month": nepali_month, "year": nepali_year, "branches": results}
        if missing:
            response["missing_branchIDs"] = missing
        return Response(response, status=status.HTTP_200_OK)

# class MonthlySalaryReportDetailAPIView(APIView):
#     """
#     GET /branch/<branch_id>/salary-report/?year=2082&month=Shrawan
#     Returns one saved report with full employee breakdown.
#     """
#     def get(self, request, branch_id):
#         nepali_year = request.query_params.get("year")
#         nepali_month = request.query_params.get("month")

#         if not all([nepali_year, nepali_month]):
#             return Response(
#                 {"error": "year and month are required."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         try:
#             branch = Branch.objects.get(id=branch_id)
#         except Branch.DoesNotExist:
#             return Response({"error": "Branch not found."}, status=status.HTTP_404_NOT_FOUND)

#         report = MonthlySalaryReport.objects.filter(
#             branch=branch, nepali_year=nepali_year, month=nepali_month
#         ).prefetch_related(
#             "employee_salaries__pay_packages",
#             "employee_salaries__leave_deductions",
#             "employee_salaries__fund_deductions",
#             "employee_salaries__tax_deductions",
#         ).first()

#         if not report:
#             return Response(
#                 {"error": "No salary report found for this branch/year/month."},
#                 status=status.HTTP_404_NOT_FOUND,
#             )

#         return Response(
#             {
#                 "reportID": report.id,
#                 "branchID": branch.id,
#                 "month": report.month,
#                 "year": report.nepali_year,
#                 "nepali_date": report.nepali_date,
#                 "fiscal_year": report.fiscal_year,
#                 "employee_count": report.employee_salaries.count(),
#                 "salary_sheets": [
#                     serialize_employee_salary(emp_salary)
#                     for emp_salary in report.employee_salaries.all()
#                 ],
#             },
#             status=status.HTTP_200_OK,
#         )


from datetime import date

import nepali_datetime
from django.db.models import Prefetch

MONTH_INDEX = {
    nepali_datetime.date(2080, m, 1).strftime("%B"): m for m in range(1, 13)
}
# MONTH_NAME = {idx: name for name, idx in MONTH_INDEX.items()}

MONTHS_TO_SHOW = 6  # present month + the 5 before it


MONTH_LOOKUP = {name.lower(): idx for name, idx in MONTH_INDEX.items()}  # keep one copy, next to MONTH_INDEX


def _report_sort_key(report):
    try:
        year = int(str(report.nepali_year).strip())
    except (TypeError, ValueError):
        year = 0
    return (year, MONTH_LOOKUP.get(str(report.month).strip().lower(), 0))


def _last_months(count):
    """[(year, month_idx), ...] starting from the present Nepali month, going backwards."""
    today = nepali_datetime.date.from_datetime_date(date.today())
    year, month = today.year, today.month
    keys = []
    for _ in range(count):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys


def _name_key(year, month):
    """(int year, lowercase month name), or None if the row is unusable."""
    try:
        return (int(year), str(month).strip().lower())
    except (ValueError, TypeError):
        return None


def build_history(branch, window, window_set):
    # window key -> (year, month name), using the names the window already displays correctly
    window_names = {key: (key[0], MONTH_NAME[key[1]].lower()) for key in window}
    wanted = set(window_names.values())

    # Cheap pass: find which saved reports fall in the window
    selected_ids = [
        r.id for r in MonthlySalaryReport.objects.filter(branch=branch)
        if _name_key(r.nepali_year, r.month) in wanted
    ]

    # Heavy pass: load employee data only for those
    reports = MonthlySalaryReport.objects.filter(id__in=selected_ids).prefetch_related(
        Prefetch(
            "employee_salaries",
            queryset=EmployeeSalary.objects.select_related("empID").order_by("name"),
        ),
        "employee_salaries__pay_packages",
        "employee_salaries__leave_deductions",
        "employee_salaries__fund_deductions",
        "employee_salaries__tax_deductions",
    )
    by_name = {_name_key(r.nepali_year, r.month): r for r in reports}

    result = []
    for key in window:  # already most recent first
        r = by_name.get(window_names[key])
        if r:
            sheets = [serialize_employee_salary(e) for e in r.employee_salaries.all()]
            result.append({
                "reportID": r.id,
                "month": r.month,
                "year": r.nepali_year,
                "nepali_date": r.nepali_date,
                "fiscal_year": r.fiscal_year,
                "status": "completed",
                "employee_count": len(sheets),
                "salary_sheets": sheets,
            })
        else:
            result.append({
                "reportID": None,
                "month": MONTH_NAME[key[1]],
                "year": str(key[0]),
                "nepali_date": None,
                "fiscal_year": None,
                "status": "in progress",
                "employee_count": 0,
                "salary_sheets": [],
            })
    return result


class MonthlySalaryReportListAPIView(APIView):
    """
    GET /branch/<branch_id>/salary-report/history/                 (one branch, old shape)
    GET /salary-report/history/?branchID=1,2,5|all                 (grouped by branch)
    Last six months (present month first).
    """
    def get(self, request, branch_id=None):
        branches, missing, error = resolve_branches(request, branch_id)
        if error:
            return error

        window = _last_months(MONTHS_TO_SHOW)
        window_set = set(window)

        if branch_id is not None:
            branch = branches[0]
            return Response(
                {"branchID": branch.id,"branch_name": branch.name, "reports": build_history(branch, window, window_set)},
                status=status.HTTP_200_OK,
            )

        response = {
            "branches": [
                {"branchID": b.id, "reports": build_history(b, window, window_set)}
                for b in branches
            ]
        }
        if missing:
            response["missing_branchIDs"] = missing
        return Response(response, status=status.HTTP_200_OK)

from collections import defaultdict

import nepali_datetime
from django.db.models import Count, Sum



def _sum_by_report(model, report_ids, name_field, value_field):
    """Returns {report_id: [{name_field: ..., value_field: sum}, ...]}"""
    rows = (
        model.objects
        .filter(employee__monthly_report_id__in=report_ids)
        .values("employee__monthly_report_id", name_field)
        .order_by(name_field)
        .annotate(sum_value=Sum(value_field))
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["employee__monthly_report_id"]].append(
            {name_field: row[name_field], value_field: row["sum_value"]}
        )
    return grouped


MONTH_NAME = {idx: name for name, idx in MONTH_INDEX.items()}


def _month_sequence(start, end):
    """Yield (year, month_idx) from start to end, inclusive."""
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month > 12:
            year, month = year + 1, 1


MONTH_LOOKUP = {name.lower(): idx for name, idx in MONTH_INDEX.items()}  # case-insensitive input


def _parse_bound(request, prefix, default_month):
    """Reads <prefix>_year / <prefix>_month. Returns (year, month_idx) or None if neither is sent."""
    year = request.query_params.get(f"{prefix}_year")
    month = request.query_params.get(f"{prefix}_month")

    if not year and not month:
        return None
    if not year:
        raise ValueError(f"{prefix}_year is required when {prefix}_month is given.")

    try:
        year = int(year)
    except ValueError:
        raise ValueError(f"Invalid {prefix}_year.")

    if month:
        month_idx = MONTH_LOOKUP.get(month.strip().lower())
        if month_idx is None:
            raise ValueError(f"Invalid {prefix}_month. Use one of: {', '.join(MONTH_INDEX)}.")
    else:
        month_idx = default_month

    return (year, month_idx)

MAX_MONTHS = 120  # safety cap so a typo like from_year=1 can't generate thousands of months
def _parse_range(request):
    lower = _parse_bound(request, "from", default_month=1)   # only year given -> start of year
    upper = _parse_bound(request, "to", default_month=12)    # only year given -> end of year
    if lower and upper and lower > upper:
        raise ValueError("The 'from' year/month must be earlier than or equal to the 'to' year/month.")
    return lower, upper


def _in_range(report, lower, upper):
    key = _report_sort_key(report)
    return (lower is None or key >= lower) and (upper is None or key <= upper)

def build_graph_months(branch, lower, upper):
    """Raises ValueError if the range is too large."""
    reports = [
        r for r in MonthlySalaryReport.objects.filter(branch=branch).annotate(
            employee_count=Count("employee_salaries")
        )
        if _in_range(r, lower, upper)
    ]
    report_ids = [r.id for r in reports]

    totals = {
        row["monthly_report_id"]: row
        for row in EmployeeSalary.objects
        .filter(monthly_report_id__in=report_ids)
        .values("monthly_report_id")
        .order_by()
        .annotate(
            gross=Sum("gross_salary"),
            total=Sum("total_salary"),
            net=Sum("net_amount"),
        )
    }

    pay_packages = _sum_by_report(EmployeeSalaryPayPackage, report_ids, "package_name", "amount")
    fund_deductions = _sum_by_report(FundDeduction, report_ids, "name", "amount")
    tax_deductions = _sum_by_report(TaxDeduction, report_ids, "name", "total")

    entries = [
        (
            _report_sort_key(r),
            {
                "reportID": r.id,
                "month": r.month,
                "year": r.nepali_year,
                "has_data": True,
                "employee_count": r.employee_count,
                "gross_salary": totals.get(r.id, {}).get("gross", 0),
                "total_salary": totals.get(r.id, {}).get("total", 0),
                "net_amount": totals.get(r.id, {}).get("net", 0),
                "pay_packages": pay_packages[r.id],
                "fund_deductions": fund_deductions[r.id],
                "tax_deductions": tax_deductions[r.id],
            },
        )
        for r in reports
    ]

    saved_keys = {key for key, _ in entries}
    valid_keys = [k for k in saved_keys if k[0] > 0 and k[1] > 0]
    start = lower or (min(valid_keys) if valid_keys else upper)
    end = upper or (max(valid_keys) if valid_keys else lower)

    if start and end:
        span = (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1
        if span > MAX_MONTHS:
            raise ValueError(f"Range too large (max {MAX_MONTHS} months).")
        for key in _month_sequence(start, end):
            if key not in saved_keys:
                entries.append((
                    key,
                    {
                        "reportID": None,
                        "month": MONTH_NAME[key[1]],
                        "year": str(key[0]),
                        "has_data": False,
                        "employee_count": 0,
                        "gross_salary": 0,
                        "total_salary": 0,
                        "net_amount": 0,
                        "pay_packages": [],
                        "fund_deductions": [],
                        "tax_deductions": [],
                    },
                ))

    entries.sort(key=lambda pair: pair[0])  # oldest -> newest
    return [entry for _, entry in entries]


class MonthlySalaryGraphAPIView(APIView):
    """
    GET /branch/<branch_id>/salary-report/graph/?from_year=..&from_month=..&to_year=..&to_month=..
    GET /salary-report/graph/?branchID=1,2,5|all&from_year=..  (grouped by branch)
    """
    def get(self, request, branch_id=None):
        branches, missing, error = resolve_branches(request, branch_id)
        if error:
            return error

        try:
            lower, upper = _parse_range(request)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if branch_id is not None:
                branch = branches[0]
                return Response(
                    {"branchID": branch.id, "branch_name": branch.name, "months": build_graph_months(branch, lower, upper)},
                    status=status.HTTP_200_OK,
                )

            response = {
                "branches": [
                    {"branchID": b.id, "branch_name": b.name, "months": build_graph_months(b, lower, upper)}
                    for b in branches
                ]
            }
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if missing:
            response["missing_branchIDs"] = missing
        return Response(response, status=status.HTTP_200_OK)

import nepali_datetime

# Build month names from the library so spellings always match what strftime("%B") gives you
# NEPALI_MONTHS = [nepali_datetime.date(2080, m, 1).strftime("%B") for m in range(1, 13)]
# MONTH_INDEX = {name.lower(): i for i, name in enumerate(NEPALI_MONTHS)}


def next_nepali_period(year, month_name):
    idx = MONTH_LOOKUP[month_name.strip().lower()]
    if idx == 12:  # Chaitra -> Baisakh of next year
        return int(year) + 1, MONTH_NAME[1]
    return int(year), MONTH_NAME[idx + 1]


def get_last_saved_period(branch_ids=None):
    """Returns (year, month_name) of the latest saved report, or None."""
    reports = MonthlySalaryReport.objects.all()
    if branch_ids is not None:
        reports = reports.filter(branch_id__in=branch_ids)

    latest = None
    for year, month in reports.values_list("nepali_year", "month").distinct():
        idx = MONTH_LOOKUP.get(str(month).strip().lower())
        if idx is None:
            continue  # skip unrecognised month names
        try:
            key = (int(str(year).strip()), idx)
        except (ValueError, TypeError):
            continue  # skip a bad year
        if latest is None or key > latest[0]:
            latest = (key, (key[0], MONTH_NAME[idx]))
    return latest[1] if latest else None


class NextSalaryPeriodAPIView(APIView):
    """
    GET /next-salary-period/?branchID=all
    GET /next-salary-period/?branchID=1,2,5      (or ?branchID=1&branchID=2)
    No branchID is treated as "all".
    """
    def get(self, request, *args, **kwargs):
        raw = request.query_params.getlist("branchID")
        tokens = [t.strip() for v in raw for t in v.split(",") if t.strip()]

        if not tokens or any(t.lower() == "all" for t in tokens):
            branch_ids = None  # all branches
        else:
            try:
                branch_ids = list(dict.fromkeys(int(t) for t in tokens))
            except ValueError:
                return Response({"error": "Invalid branchID."}, status=status.HTTP_400_BAD_REQUEST)

        nepali_today = nepali_datetime.date.from_datetime_date(date.today())
        last_saved = get_last_saved_period(branch_ids)

        if last_saved:
            next_year, next_month = next_nepali_period(*last_saved)
        else:
            next_year, next_month = nepali_today.year, nepali_today.strftime("%B")

        today_key = (nepali_today.year, nepali_today.month)               # 1-based now
        next_key = (next_year, MONTH_LOOKUP[next_month.lower()])          # 1-based now

        return Response({
            "branchID": "all" if branch_ids is None else branch_ids,
            "last_saved": {"year": last_saved[0], "month": last_saved[1]} if last_saved else None,
            "next_period": {"year": next_year, "month": next_month},
            "is_future": next_key > today_key,
        }, status=status.HTTP_200_OK)


from collections import defaultdict
from decimal import Decimal

from django.db.models import Prefetch
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from organization.utils import resolve_branches
# plus your existing: MonthlySalaryReport, EmployeeSalary, _parse_range, _in_range,
# _report_sort_key, MAX_MONTHS, MONTH_NAME


def _d(value):
    """Safe Decimal from a number, string or None."""
    try:
        return Decimal(str(value)) if value is not None else Decimal("0")
    except Exception:
        return Decimal("0")


def _new_employee_entry(es, report):
    emp = es.empID
    return {
        "empID": es.empID_id,
        "name": es.name or (emp.name if emp else None),
        "department": emp.department if emp else None,
        "branchID": report.branch_id,
        "branch_name": report.branch.name,
        "months_count": 0,
        "gross_salary": Decimal("0"),
        "total_salary": Decimal("0"),
        "net_amount": Decimal("0"),
        "unpaid_leaves": Decimal("0"),
        "unpaid_leavesdeduction": Decimal("0"),
        "_pay_packages": defaultdict(Decimal),
        "_fund_deductions": defaultdict(Decimal),
        "_tax_deductions": defaultdict(Decimal),
        "months": [],
    }


def _named_totals(totals, amount_key):
    return [{"name": name, amount_key: float(amount)} for name, amount in totals.items()]


class EmployeeSalaryRangeAPIView(APIView):
    """
    GET /branch/salary-report/employees/
        ?branchID=1,2,5|all
        &from_year=2083&from_month=Asar&to_year=2083&to_month=Kartik
        [&emp_ID=12]            only one employee
        [&include_months=true]  adds a month-by-month list for each employee

    Employee-wise totals over the saved salary reports in the range.
    """
    def get(self, request, branch_id=None):
        branches, missing, error = resolve_branches(request, branch_id)
        if error:
            return error

        try:
            lower, upper = _parse_range(request)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if lower and upper:
            span = (upper[0] - lower[0]) * 12 + (upper[1] - lower[1]) + 1
            if span > MAX_MONTHS:
                return Response(
                    {"error": f"Range too large (max {MAX_MONTHS} months)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        emp_id = request.query_params.get("emp_ID")
        if emp_id:
            try:
                emp_id = int(emp_id)
            except ValueError:
                return Response({"error": "emp_ID must be a number."}, status=status.HTTP_400_BAD_REQUEST)

        include_months = request.query_params.get("include_months", "").lower() in ("1", "true", "yes")

        # 1. Saved reports of the chosen branches that fall inside the range
        reports = [
            r for r in MonthlySalaryReport.objects.filter(branch__in=branches).select_related("branch")
            if _in_range(r, lower, upper)
        ]
        reports.sort(key=_report_sort_key)  # oldest first, so each employee's months come out in order
        report_by_id = {r.id: r for r in reports}

        # 2. All employee rows for those reports, loaded in one go
        salaries = EmployeeSalary.objects.filter(monthly_report_id__in=report_by_id.keys())
        if emp_id:
            salaries = salaries.filter(empID_id=emp_id)
        salaries = salaries.select_related("empID").prefetch_related(
            "pay_packages", "leave_deductions", "fund_deductions", "tax_deductions"
        )

        # 3. Order rows by their report's month, then add up per employee
        rows = sorted(salaries, key=lambda s: _report_sort_key(report_by_id[s.monthly_report_id]))
        employees = {}
        for es in rows:
            report = report_by_id[es.monthly_report_id]
            key = es.empID_id if es.empID_id else f"name:{es.name}"
            entry = employees.get(key)
            if entry is None:
                entry = employees[key] = _new_employee_entry(es, report)

            month_unpaid = sum((_d(ld.unpaid_leaves) for ld in es.leave_deductions.all()), Decimal("0"))
            month_unpaid_amt = sum((_d(ld.unpaid_leavesdeduction) for ld in es.leave_deductions.all()), Decimal("0"))

            entry["months_count"] += 1
            entry["gross_salary"] += _d(es.gross_salary)
            entry["total_salary"] += _d(es.total_salary)
            entry["net_amount"] += _d(es.net_amount)
            entry["unpaid_leaves"] += month_unpaid
            entry["unpaid_leavesdeduction"] += month_unpaid_amt

            for pp in es.pay_packages.all():
                entry["_pay_packages"][pp.package_name] += _d(pp.amount)
            for fd in es.fund_deductions.all():
                entry["_fund_deductions"][fd.name] += _d(fd.amount)
            for td in es.tax_deductions.all():
                entry["_tax_deductions"][td.name] += _d(td.total)

            if include_months:
                entry["months"].append({
                    "reportID": report.id,
                    "month": report.month,
                    "year": report.nepali_year,
                    "gross_salary": float(_d(es.gross_salary)),
                    "total_salary": float(_d(es.total_salary)),
                    "net_amount": float(_d(es.net_amount)),
                    "unpaid_leaves": float(month_unpaid),
                    "unpaid_leavesdeduction": float(month_unpaid_amt),
                })

        # 4. Turn the working entries into the response
        result = []
        for entry in employees.values():
            pay_packages = entry.pop("_pay_packages")
            fund_deductions = entry.pop("_fund_deductions")
            tax_deductions = entry.pop("_tax_deductions")

            entry["pay_packages"] = _named_totals(pay_packages, "amount")
            entry["fund_deductions"] = _named_totals(fund_deductions, "amount")
            entry["tax_deductions"] = _named_totals(tax_deductions, "total")
            entry["total_fund_deduction"] = float(sum(fund_deductions.values(), Decimal("0")))
            entry["total_tax_deduction"] = float(sum(tax_deductions.values(), Decimal("0")))

            for field in ("gross_salary", "total_salary", "net_amount",
                          "unpaid_leaves", "unpaid_leavesdeduction"):
                entry[field] = float(entry[field])
            if not include_months:
                entry.pop("months")
            result.append(entry)

        result.sort(key=lambda e: (e["branchID"], e["name"] or ""))

        response = {
            "from": {"year": lower[0], "month": MONTH_NAME[lower[1]]} if lower else None,
            "to": {"year": upper[0], "month": MONTH_NAME[upper[1]]} if upper else None,
            "reports_found": len(reports),
            "employee_count": len(result),
            "employees": result,
        }
        if missing:
            response["missing_branchIDs"] = missing
        return Response(response, status=status.HTTP_200_OK)