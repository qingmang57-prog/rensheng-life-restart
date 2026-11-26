import os
import sys
import uuid
import unittest
from pathlib import Path
from resource_probe import monitored_run,hardware


class ResourceProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows resource sampler')
    def test_child_process_memory_is_observed(self):
        folder=Path(__file__).resolve().parent/'.test-runtime'/('probe-'+uuid.uuid4().hex)
        child='import time; data=bytearray(32*1024*1024); time.sleep(1.2)'
        parent='import subprocess,sys; subprocess.run([sys.executable,"-c",'+repr(child)+'],check=True)'
        result=monitored_run([sys.executable,'-c',parent],folder,'memory-child',interval=.05)
        self.assertEqual(result['status'],'succeeded')
        self.assertGreaterEqual(result['observed_processes'],2)
        self.assertGreater(result['observed_tree_rss_peak_mib'],35)
        self.assertGreater(result['observed_tree_private_peak_mib'],25)
        self.assertTrue((folder/'memory-child.log').is_file())

    def test_hardware_does_not_claim_missing_gpu_as_zero(self):
        info=hardware()
        self.assertGreater(info['logical_cpus'],0)
        self.assertIn('gpu',info)
        if info['gpu'] is not None:self.assertIsInstance(info['gpu'],list)


if __name__=='__main__':unittest.main(verbosity=2)
