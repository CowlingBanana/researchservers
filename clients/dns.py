import inspect
import logging
import os
import random
import re

from twisted.internet import reactor
from twisted.names import dns, client

from jsonroutes import JsonRoutes
from utils import get_ipv4_address, get_ipv6_address, exec_cached_script

logger = logging.getLogger()

# Shim classes for DNS records that twisted does not support
record_classes = {k.split("_", 1)[1] : v for k,v in inspect.getmembers(dns, lambda x: inspect.isclass(x) and x.__name__.startswith("Record_"))}

class Record_CAA:
    """
    The Certification Authority Authorization record.
    """
    TYPE = 257
    fancybasename = 'CAA'

    def __init__(self, record, ttl=None):
        record = record.split(b' ', 2)
        self.flags = int(record[0])
        self.tag = record[1]
        self.value = record[2].replace(b'"', b'')
        self.ttl = dns.str2time(ttl)

    def encode(self, strio, compDict = None):
        strio.write(bytes([self.flags, len(self.tag)]))
        strio.write(self.tag)
        strio.write(self.value)

    def decode(self, strio, length = None):
        pass

    def __hash__(self):
        return hash(self.address)

    def __str__(self):
        return '<CAA record=%d %s "%s" ttl=%s>' % (self.flags, self.tag.decode(), self.value.decode(), self.ttl)
    __repr__ = __str__

dns.QUERY_TYPES[Record_CAA.TYPE] = Record_CAA.fancybasename
dns.REV_TYPES[Record_CAA.fancybasename] = Record_CAA.TYPE
record_classes[Record_CAA.fancybasename] = Record_CAA

class DNSJsonClient(client.Resolver):
    """
    DNS resolver which responds to dns queries based on a JsonRoutes object
    """

    noisy = False

    def __init__(self, domain, ipv4_address=None, ipv6_address=None):
        self.domain = domain
        self.ipv4_address = ipv4_address or get_ipv4_address()
        self.ipv6_address = ipv6_address or get_ipv6_address()
        self.routes = JsonRoutes(protocol="dns", domain=self.domain)
        self.replace_args = {"domain": self.domain, "ipv4": self.ipv4_address, "ipv6": self.ipv6_address}
        super().__init__(servers=[("8.8.8.8", 53)])

    def _lookup(self, lookup_name, lookup_cls, lookup_type, timeout):
        records = []
        record_type = None

        str_lookup_name = lookup_name.decode("UTF-8").lower()

        # Access route_descriptors directly to perform complex filtering
        for route_descriptor in self.routes.route_descriptors:
            if re.search(route_descriptor["route"], str_lookup_name):
                if lookup_cls == route_descriptor.get("class", dns.IN):

                    # Convert the route_descriptor type to an integer
                    rd_type = route_descriptor.get("type", str(lookup_type))
                    if rd_type.isdigit():
                        rd_type = int(rd_type)
                        rd_type_name = dns.QUERY_TYPES.get(rd_type, "UnknownType")
                    else:
                        rd_type_name = rd_type
                        rd_type = dns.REV_TYPES.get(rd_type_name, 0)

                    # If the lookup type matches the reoute descriptor type
                    if lookup_type == rd_type:
                        logger.debug("Matched route {}".format(repr(route_descriptor)))
                        ttl = int(route_descriptor.get("ttl", "60"))
                        record_type = lookup_type
                        record_class = record_classes.get(rd_type_name, dns.UnknownRecord)

                        # Determine the record type and record type class
                        if "record" in route_descriptor:
                            record_type = dns.REV_TYPES.get(route_descriptor["record"], 0)
                            record_class = record_classes.get(route_descriptor["record"], dns.UnknownRecord)

                        # Obtain an array of responses
                        responses = [self.ipv6_address if rd_type_name == "AAAA" else self.ipv4_address]
                        if "response" in route_descriptor:
                            responses = route_descriptor["response"]

                        if "script" in route_descriptor:
                            try:
                                args = route_descriptor.get("args", [])
                                kwargs = route_descriptor.get("kwargs", {})
                                get_record = exec_cached_script(route_descriptor["script"])["get_record"]
                                responses = get_record(lookup_name, lookup_cls, lookup_type, *args, **kwargs)
                            except Exception as e:
                                logger.exception("Error executing script {}".format(route_descriptor["script"]))

                        if isinstance(responses, str):
                            responses = [responses]
                        for response in responses if not route_descriptor.get("random", False) else [random.choice(responses)]:
                            # Replace regex groups in the route path
                            for i, group in enumerate(re.search(route_descriptor["route"], str_lookup_name).groups()):
                                if group is not None:
                                    response = response.replace("${}".format(i + 1), group)
                            response = response.format(**self.replace_args).encode()
                            records.append((lookup_name, record_type, lookup_cls, ttl, record_class(response, ttl=ttl)))
                        break

        if len(records):
            try:
                return [tuple([dns.RRHeader(*x) for x in records]) ,(), ()]
            except:
                logger.exception("Unhandled exception with response")
        return [(), (), ()]
