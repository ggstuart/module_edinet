from mrjob.job import MRJob
from mrjob.protocol import PickleValueProtocol

# hbase and mongo clients libs
import happybase
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson.objectid import ObjectId

# Generic imports
import glob
from json import load
from datetime import datetime
import numpy as np

class Hadoop_ETL(MRJob):
    
    INPUT_PROTOCOL = PickleValueProtocol

    def mapper_init(self):
        
        # recover json configuration uploaded with script
        fn = glob.glob('*.json')
        self.config = load(open(fn[0]))
        
        self.readings_cache = {}
        self.devices_cache = {} #careful: consider 1 Million contracts with 5 devices each it will take 1GB on memory [[ (48*2+100)*5 * 1000000 / 1024 / 1024 = 934 MB  ]]
        
        # open connections
        self.hbase = happybase.Connection(self.config['hbase']['host'], self.config['hbase']['port'])
        self.hbase.open()
        
        self.tables_list = self.hbase.tables()
        
        self.mongo = MongoClient(self.config['mongodb']['host'], self.config['mongodb']['port'])
        self.mongo[self.config['mongodb']['db']].authenticate(
                self.config['mongodb']['username'],
                self.config['mongodb']['password']
                )
        
        #if not 'amon_measures_measurements_with_errors' in self.mongo[self.config['app']['mongodb']['db']].collection_names():
        #        self.mongo[self.config['app']['mongodb']['db']].create_collection('amon_measures_measurements_with_errors')
        #        self.mongo[self.config['app']['mongodb']['db']]['amon_measures_measurements_with_errors'].create_index([("companyId", ASCENDING),("timestamp", DESCENDING)])
        
        
    def add_reading_information(self, doc):
        r = self.readings_cache.get(doc['reading'])
        if not r:
            #r = self.mongo[self.config['app']['mongodb']['db']]['readings'].find_one({'_id': doc['reading']})
            r = self.mongo[self.config['mongodb']['db']]['readings'].find_one({'_id': ObjectId(doc['reading'])})
            self.readings_cache[doc['reading']] = r
        
        if r is None:
            doc['error'] = "No reading information related"
            return doc
        
        doc['reading'] = r
        
        return doc
    
    def build_row_key(self, doc):
        row_key = []
        for element in self.config['hbase_table']['key']:
            row_key.append(str(doc[element]))
            #row_key.append(element)
            
        return "~".join(row_key)


    def datetime_to_timestamp(self, doc, field):

        doc[field] = int(doc[field].replace(tzinfo=pytz.UTC).strftime('%s'))
        # Input data is always in UTC and the timestamp stored in HBase must be in UTC timezone.

        return doc

    def convert_units_to_kilo(self, unit, value):
        conversions = ['w', 'wh', 'varh', 'va', 'var', 'whth']
        conversions_M = ['mw', 'mwh', 'mvarh', 'mva', 'mvar', 'mwhth']
        try:
            # Watts, WattsHour, VoltAmpHour, VoltAmps, VoltAmpsReactive,WattHoursofHeath
            i = conversions.index(unit.lower())
            value /= 1000.0
        except ValueError:
            pass
        try:
            # Watts, WattsHour, VoltAmpHour, VoltAmps, VoltAmpsReactive,WattHoursofHeath
            i = conversions_M.index(unit.lower())
            value *= 1000.0
        except ValueError:
            pass
        return value

    def add_ts_bucket(self, doc, field_b, field_ts):
        doc[field_b] = (doc[field_ts] / 100) % 100
        return doc

    def is_float(self, x):
        try:
            x = float(x)
            return np.isfinite(x)
        except:
            return False

        
    def mapper(self, _, doc):   #we don't have value -> input protocol pickleValue which means no key is read
        
        """
        doc = {
            "timestamp": "2013-11-30 18:00:00",
            "reading": "52a9845fdfeb570207c02319",
            "deviceId": "912062bb-21ec-5787-805d-cf3858c67405",
            "values": {"p1":12,"p2":145,"p3":543,"p4":56,"p6":2},
            "companyId": "1234509876"
            }
        """
        
        # create a dictionary from python string
        # use config file uploaded with script
        #doc = self.list_to_doc(line)
        
        # Transform functions
        doc = self.add_reading_information(doc)
        #doc = self.translate_deviceId_contractId(doc)
        
        if 'error' in doc:
            #If there are errors with the doc, save the measure in the amon_measure_measurements_with_errors collection in REST
            doc['reading'] = doc['reading']['_id']
            doc['error_detected_at'] = datetime.now()
            #self.mongo[self.config['app']['mongodb']['db']]['amon_measures_measurements_with_errors'].insert(doc)
            yield 1, str(doc) # yielding records will tell us if something went wrong (output map records should be 0)
            #yield 2, str(doc['query'])
            # customer not found
            #raise Exception("The deviceId %s dont correspond to any contractId" % doc['deviceId'])
            return
        
        doc = self.datetime_to_timestamp(doc,'timestamp')
        doc = self.add_ts_bucket(doc, 'bucket', 'timestamp')
        
        # ROW KEY DEFINITION
        row_key = self.build_row_key(doc)
        
        # Key - Value lists from the values dictionary
        doc_key = ['m:'+str(item[0]) if doc['reading']['period'] in ['INSTANT','PULSE'] else 'm:'+str(item[0])+'a' for item in doc['values'].iteritems() if self.is_float(str(item[1]))]
        doc_val = [self.convert_units_to_kilo(doc['reading']['unit'], item) for item in doc['values'].itervalues() if self.is_float(str(item))]
        
        # Extend the Key - Value lists with the aggregated consumption of all the periods in case of INSTANT or PULSE period
        if doc['reading']['period'] in ['INSTANT','PULSE']:
            doc_key.extend(['m:v','m:calc']) # column value, column calculated
            doc_val.extend([np.sum(doc_val),'0']) # Sum all instant period values, Consider a 0 because the measure is not treated with the cumulative to instant ETL
        
        # ROW VALUE DEFINITION
        row = {}
        for i in xrange(len(doc_key)):
            row[doc_key[i]] = str(doc_val[i])
        
        table_name = doc['reading']['type']+'_'+str(doc['companyId'])
        
        try:
            if not table_name in self.tables_list:
                self.hbase.create_table(table_name, { 'm': dict() })
                self.tables_list.append(table_name)
        except:
            pass
            
        hbase_table = self.hbase.table(table_name)
            
        hbase_table.put(row_key, row)
        
        #yield row_key, str(row, table_name)  
    
    
    
if __name__ == '__main__':
    Hadoop_ETL.run()    