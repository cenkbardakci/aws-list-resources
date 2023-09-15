#!/usr/bin/env python3

import argparse
import bisect
import boto3
import botocore.config
import concurrent.futures
import datetime
import json
import random
import sys


BOTO_CONFIG = botocore.config.Config(retries={"total_max_attempts": 5, "mode": "standard"})

MAX_THREADS_FOR_REGIONS = 8


class DeniedListOperationException(Exception):
    """
    Raised when the "List" operation of the Cloud Control API failed due to permission errors.
    """

    pass


def get_supported_resource_types(cloudformation_client):
    """
    Returns a list of resource types that are supported in a region by querying the CloudFormation registry.
    Examples: AWS::EC2::RouteTable, AWS::IAM::Role, AWS::KMS::Key, etc.
    """
    resource_types = set()

    list_types_paginator = cloudformation_client.get_paginator("list_types")
    for provisioning_type in ("FULLY_MUTABLE", "IMMUTABLE"):
        for list_types_page in list_types_paginator.paginate(
            Type="RESOURCE",
            Visibility="PUBLIC",
            ProvisioningType=provisioning_type,
            DeprecatedStatus="LIVE",
            Filters={"Category": "AWS_TYPES"},
        ):
            for type in list_types_page["TypeSummaries"]:
                resource_types.add(type["TypeName"])

    return list(resource_types)


def get_resources(cloudcontrol_client, resource_type):
    """
    Returns a list of discovered resources of the given resource type. Uses the "List" operation of the Cloud Control
    API. If the API call failed, an empty list is returned. If the API call likely failed because of permission
    issues, a DeniedListOperationException is raised.
    """
    print("{}, {}".format(cloudcontrol_client._client_config.region_name, resource_type))
    collected_resources = []

    list_resources_paginator = cloudcontrol_client.get_paginator("list_resources")
    try:
        for list_resources_page in list_resources_paginator.paginate(TypeName=resource_type):
            for resource in list_resources_page["ResourceDescriptions"]:
                collected_resources.append(resource["Identifier"])

    except Exception as ex:
        # There is unfortunately a long and non-uniform list of exceptions that can occur with the Cloud Control API,
        # presumably because it just passes through the exceptions of the underlying services. Examples for when the
        # "List" operation requires additional parameters or when the caller lacks permissions for an API call:
        # UnsupportedActionException, InvalidRequestException, GeneralServiceException, ResourceNotFoundException,
        # HandlerInternalFailureException, AccessDeniedException, AuthorizationError, etc. They are thus handled by
        # this broad except clause. The end result is the same: resources for this resource type cannot be listed.

        exception_msg = str(ex).lower()
        for keyword in ("denied", "authorization", "authorized"):
            if keyword in exception_msg:
                raise DeniedListOperationException()

    return sorted(collected_resources)


def analyze_region(region):
    """
    Lists all resources of resources types that are supported in the region and adds them to the result collection.
    """
    boto_session = boto3.session.Session(profile_name=profile, region_name=region)

    # Create a shuffled list of resource types that are supported in the region. Shuffling avoids API throttling when
    # listing the resources (e.g., avoid querying all resources of the EC2 API namespace within only a few seconds)
    cloudformation_client = boto_session.client("cloudformation", config=BOTO_CONFIG)
    resource_types = get_supported_resource_types(cloudformation_client)
    random.shuffle(resource_types)

    # List the resources of each resource type
    cloudcontrol_client = boto_session.client("cloudcontrol", config=BOTO_CONFIG)
    for resource_type in resource_types:
        try:
            resources = get_resources(cloudcontrol_client, resource_type)
            if resources:
                if only_show_counts:
                    result_collection["regions"][region][resource_type] = len(resources)
                else:
                    result_collection["regions"][region][resource_type] = resources

        except DeniedListOperationException:
            bisect.insort(result_collection["_metadata"]["denied_list_operations"][region], resource_type)


if __name__ == "__main__":
    # Check runtime environment
    if sys.version_info[0] < 3:
        print("Python version 3 required")
        sys.exit(1)

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only-show-counts",
        required=False,
        default=False,
        action="store_true",
        help="only show resource counts instead of listing their full identifiers",
    )
    parser.add_argument("--profile", required=False, nargs=1, help="optional named AWS profile to use")
    parser.add_argument("--regions", required=True, nargs=1, help="comma-separated list of target AWS regions")

    args = parser.parse_args()
    only_show_counts = args.only_show_counts
    profile = args.profile[0] if args.profile else None
    target_regions = [region for region in args.regions[0].split(",") if region]

    # Test for valid credentials
    boto_session = boto3.session.Session(profile_name=profile)
    sts_client = boto_session.client("sts", config=BOTO_CONFIG)
    try:
        sts_response = sts_client.get_caller_identity()
    except:
        print("No or invalid AWS credentials configured")
        sys.exit(1)

    # Prepare result collection structure
    run_timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    result_collection = {
        "_metadata": {
            "account_id": sts_response["Account"],
            "account_principal": sts_response["Arn"],
            "denied_list_operations": {},
            "run_timestamp": run_timestamp,
        },
        "regions": {},
    }
    for region in target_regions:
        result_collection["regions"][region] = {}
        result_collection["_metadata"]["denied_list_operations"][region] = []

    # Collect resources using a separate thread for each target region
    print("Analyzing account ID {}".format(sts_response["Account"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS_FOR_REGIONS) as executor:
        for region in target_regions:
            executor.submit(analyze_region, region)

    # Write result file
    output_file_name = "resources_{}_{}.json".format(sts_response["Account"], run_timestamp)
    with open(output_file_name, "w") as out_file:
        json.dump(result_collection, out_file, indent=2, sort_keys=True)

    print("Output file written to {}".format(output_file_name))
